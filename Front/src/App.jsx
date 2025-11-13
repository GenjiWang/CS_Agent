import React, { useState, useRef, useEffect } from 'react'

export default function App(){
    const [messages, setMessages] = useState([
        { id: 1, role: 'assistant', text: '歡迎！請輸入你的問題。' }
    ])
    const [input, setInput] = useState('')
    const [isLoading, setIsLoading] = useState(false)
    const panelRef = useRef(null)

    const wsRef = useRef(null)                  // WebSocket 實例
    const pendingAssistantId = useRef(null)     // 正在填充的 assistant 訊息 id
    const reconnectAttempts = useRef(0)         // 重連嘗試次數
    const heartbeatRef = useRef({ timer: null, missed: 0 })
    const NEXT_ID = () => Date.now() + Math.floor(Math.random() * 1000)

    // 自動滾到底
    useEffect(() => {
        if (panelRef.current) panelRef.current.scrollTop = panelRef.current.scrollHeight
    }, [messages])

    // 組出 ws url（根據當前協議切換 ws/wss）
    const wsUrl = (window.location.protocol === 'https:' ? 'wss' : 'ws') + '://localhost:8000/ws/chat'

    // 建立或重建 WebSocket
    function connectWs(url = wsUrl) {
        // 若已有連線且正在連或已開啟，則跳過
        const existing = wsRef.current
        if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return

        try {
            const ws = new WebSocket(url)
            wsRef.current = ws

            ws.onopen = () => {
                console.log('[ws] open')
                reconnectAttempts.current = 0
                startHeartbeat()
            }

            ws.onmessage = (evt) => {
                // 嘗試解析 JSON，若為 ping/pong 則處理心跳
                try {
                    const payload = JSON.parse(evt.data)
                    if (payload && payload.type === 'pong') {
                        // 收到 pong，重置 missed 計數
                        heartbeatRef.current.missed = 0
                        return
                    }
                    handleWsPayload(payload)
                } catch (err) {
                    console.error('[ws] parse error', err, evt.data)
                }
            }

            ws.onclose = (ev) => {
                console.log('[ws] closed', ev)
                stopHeartbeat()
                // 若不是主動關閉，排程重連
                if (!ev.wasClean) scheduleReconnect(url)
            }

            ws.onerror = (e) => {
                console.error('[ws] error', e)
            }
        } catch (e) {
            console.error('[ws] connect failed', e)
            scheduleReconnect(url)
        }
    }

    // 等待 WebSocket 進入 OPEN 狀態（timeout ms）
    function waitForWsOpen(ws, timeout = 3000) {
        return new Promise((resolve, reject) => {
            if (!ws) return reject(new Error('No WebSocket'))
            if (ws.readyState === WebSocket.OPEN) return resolve()
            const onOpen = () => { cleanup(); resolve() }
            const onClose = () => { cleanup(); reject(new Error('WebSocket closed before open')) }
            const onError = (err) => { cleanup(); reject(err || new Error('WebSocket error before open')) }
            const timer = setTimeout(() => { cleanup(); reject(new Error('WebSocket open timeout')) }, timeout)
            function cleanup() {
                clearTimeout(timer)
                ws.removeEventListener('open', onOpen)
                ws.removeEventListener('close', onClose)
                ws.removeEventListener('error', onError)
            }
            ws.addEventListener('open', onOpen)
            ws.addEventListener('close', onClose)
            ws.addEventListener('error', onError)
        })
    }

    // 心跳機制（每 20s 發 ping，若三次沒回 pong 則強制重連）
    function startHeartbeat() {
        stopHeartbeat()
        heartbeatRef.current.missed = 0
        heartbeatRef.current.timer = setInterval(() => {
            const ws = wsRef.current
            if (!ws || ws.readyState !== WebSocket.OPEN) return
            try {
                // 送 ping；後端若不處理 ping 可忽略
                ws.send(JSON.stringify({ type: 'ping' }))
                heartbeatRef.current.missed += 1
                if (heartbeatRef.current.missed > 2) {
                    console.warn('[ws] missed pong threshold, closing to trigger reconnect')
                    ws.close()
                }
            } catch (e) {
                console.error('[ws] heartbeat send failed', e)
            }
        }, 20000)
    }

    function stopHeartbeat() {
        if (heartbeatRef.current.timer) {
            clearInterval(heartbeatRef.current.timer)
            heartbeatRef.current.timer = null
        }
        heartbeatRef.current.missed = 0
    }

    // 指數退避重連
    function scheduleReconnect(url = wsUrl) {
        reconnectAttempts.current = Math.min(10, reconnectAttempts.current + 1)
        const attempt = reconnectAttempts.current
        const delay = Math.min(30000, 200 * 2 ** attempt)
        console.log(`[ws] schedule reconnect attempt ${attempt} in ${delay}ms`)
        setTimeout(() => connectWs(url), delay)
    }

    // 處理後端 payload（delta/done/error）
    function handleWsPayload(payload) {
        if (!payload || typeof payload !== 'object') return
        if (payload.type === 'delta') {
            const aid = pendingAssistantId.current
            if (!aid) {
                const newId = NEXT_ID()
                pendingAssistantId.current = newId
                setMessages(prev => [...prev, { id: newId, role: 'assistant', text: payload.text || '' }])
                return
            }
            setMessages(prev => prev.map(m => m.id === aid ? { ...m, text: m.text + (payload.text || '') } : m))
        } else if (payload.type === 'done') {
            pendingAssistantId.current = null
            setIsLoading(false)
        } else if (payload.type === 'error') {
            const aid = pendingAssistantId.current
            const errText = payload.error || '伺服器錯誤'
            if (aid) {
                setMessages(prev => prev.map(m => m.id === aid ? { ...m, text: errText } : m))
            } else {
                setMessages(prev => [...prev, { id: NEXT_ID(), role: 'assistant', text: errText }])
            }
            pendingAssistantId.current = null
            setIsLoading(false)
        } else {
            // 非標準訊息（例如後端回傳完整結構）
            console.warn('[ws] unknown payload', payload)
        }
    }

    // 傳送訊息（建立 init payload）
    async function sendMessage() {
        const trimmed = input.trim()
        if (!trimmed) return

        // 若已有未完成的 assistant 回覆，避免覆寫（可選行為）
        if (pendingAssistantId.current) {
            // 你可以選擇排隊或取消前一個，這裏簡單阻止並顯示訊息
            setMessages(prev => [...prev, { id: NEXT_ID(), role: 'assistant', text: '請等待前一則回覆完畢' }])
            return
        }

        const userMsg = { id: NEXT_ID(), role: 'user', text: trimmed }
        setMessages(prev => [...prev, userMsg])
        setInput('')
        setIsLoading(true)

        // 插入空的 assistant 訊息，等待 stream 填充
        const assistantId = NEXT_ID()
        pendingAssistantId.current = assistantId
        setMessages(prev => [...prev, { id: assistantId, role: 'assistant', text: '' }])

        // 確保 WebSocket 已連上
        try {
            if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
                connectWs(wsUrl)
                await waitForWsOpen(wsRef.current, 5000)
            }
        } catch (e) {
            console.error('[ws] not connected', e)
            setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, text: '無法連線，請稍後再試。' } : m))
            pendingAssistantId.current = null
            setIsLoading(false)
            return
        }

        // 發送 init payload（一次）
        try {
            const ws = wsRef.current
            const payload = {
                model: 'gpt-oss:20b',
                messages: [{ role: 'user', content: trimmed }],
                // x_api_key: 'your-api-key-if-needed'
            }
            ws.send(JSON.stringify(payload))
            // 後端會開始逐段回傳，handleWsPayload 處理追加
        } catch (err) {
            console.error('[ws] send error', err)
            setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, text: '送出失敗，請稍後再試。' } : m))
            pendingAssistantId.current = null
            setIsLoading(false)
        }
    }

    // 初次掛載建立連線
    useEffect(() => {
        connectWs(wsUrl)
        return () => {
            try {
                if (wsRef.current) wsRef.current.close()
            } catch (e) {}
            stopHeartbeat()
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    return (
        <div className="app">
            <header className="header">
                <div className="container">
                    <div>
                        <h1>智慧聊天機器人</h1>
                        <div className="meta">建立 Ollama 的智慧聊天系統</div>
                    </div>
                </div>
            </header>

            <main className="chat-area">
                <div className="inner">
                    <div className="chat-panel" ref={panelRef} style={{ maxHeight: '60vh', overflowY: 'auto' }}>
                        {messages.length === 0 ? (
                            <div className="empty">還沒有訊息，請輸入開始對話。</div>
                        ) : (
                            messages.map(msg => (
                                <div
                                    key={msg.id}
                                    className={`msg-row ${msg.role === 'user' ? 'user' : 'assistant'}`}
                                >
                                    <div className={`msg ${msg.role === 'user' ? 'user' : 'assistant'}`}>
                                        {msg.text}
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                </div>
            </main>

            <footer className="composer">
                <form
                    className="row"
                    onSubmit={e => {
                        e.preventDefault()
                        if (!isLoading && input.trim()) sendMessage()
                    }}
                >
                    <input
                        className="input"
                        type="text"
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        placeholder="請輸入您的問題..."
                        disabled={isLoading}
                        aria-label="輸入訊息"
                        onKeyDown={e => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                                e.preventDefault()
                                if (!isLoading && input.trim()) sendMessage()
                            }
                        }}
                    />
                    <button className="btn-send" type="submit" disabled={isLoading || !input.trim()}>
                        {isLoading ? '傳送中...' : '發送'}
                    </button>
                </form>
            </footer>
        </div>
    )
}
