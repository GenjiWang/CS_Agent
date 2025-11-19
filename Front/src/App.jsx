import React, { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeSanitize from 'rehype-sanitize'
import { defaultSchema } from 'hast-util-sanitize'
import DOMPurify from 'dompurify'
import 'katex/dist/katex.min.css' // KaTeX 樣式

// 擴充 sanitize schema，允許 KaTeX 會用到的 class 與 style
const katexAllowed = {
    ...defaultSchema,
    attributes: {
        ...defaultSchema.attributes,
        // 允許 span/div 的 class 與 style（KaTeX 會產生許多 span）
        span: [
            ...(defaultSchema.attributes && defaultSchema.attributes.span ? defaultSchema.attributes.span : []),
            'class',
            'className',
            'style'
        ],
        div: [
            ...(defaultSchema.attributes && defaultSchema.attributes.div ? defaultSchema.attributes.div : []),
            'class',
            'className',
            'style'
        ],
        // 若 KaTeX 生成 MathML，允許少量必要屬性
        math: ['xmlns'],
        annotation: ['encoding']
    }
}

/**
 * MarkdownViewer
 * - 支援 GFM 與 LaTeX（remark-math + rehype-katex）
 * - 先用 DOMPurify 清理輸入 markdown（避免惡意 HTML），再由 rehypeSanitize（katexAllowed）保護 KaTeX 生成的 HTML
 */
function MarkdownViewer({ source }) {
    // source 很可能來自後端流式累積；如果來源是未信任的，先用 DOMPurify sanitize
    const safeSource = typeof source === 'string' ? DOMPurify.sanitize(source) : source

    return (
        <ReactMarkdown
            children={safeSource}
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[
                rehypeKatex,                 // 先把 LaTeX 轉成 HTML
                [rehypeSanitize, katexAllowed] // 然後以擴充過的 schema 做 sanitize（允許 KaTeX 標籤/屬性）
            ]}
        />
    )
}

export default function App() {
    const [messages, setMessages] = useState([
        { id: 1, role: 'assistant', text: '歡迎！請輸入你的問題。' }
    ])
    const [input, setInput] = useState('')
    const [isLoading, setIsLoading] = useState(false)
    const panelRef = useRef(null)

    const wsRef = useRef(null)
    const pendingAssistantId = useRef(null)
    const reconnectAttempts = useRef(0)
    const heartbeatRef = useRef({ timer: null, missed: 0 })
    const bufferRef = useRef('')           // accumulate small deltas
    const flushTimerRef = useRef(null)
    const NEXT_ID = () => Date.now() + Math.floor(Math.random() * 1000)

    // auto-scroll
    useEffect(() => {
        if (panelRef.current) panelRef.current.scrollTop = panelRef.current.scrollHeight
    }, [messages])

    // build ws url (local dev)
    const wsUrl = (window.location.protocol === 'https:' ? 'wss' : 'ws') + '://127.0.0.1:8000/ws/chat'

    const connectWs = useCallback((url = wsUrl) => {
        const existing = wsRef.current
        if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return

        try {
            const ws = new WebSocket(url)
            wsRef.current = ws

            ws.onopen = () => {
                reconnectAttempts.current = 0
                startHeartbeat()
            }

            ws.onmessage = (evt) => {
                try {
                    const payload = JSON.parse(evt.data)
                    if (payload && payload.type === 'pong') {
                        heartbeatRef.current.missed = 0
                        return
                    }
                    handleWsPayload(payload)
                } catch (err) {
                    console.error('[ws] parse error', err, evt.data)
                }
            }

            ws.onclose = (ev) => {
                stopHeartbeat()
                if (!ev.wasClean) scheduleReconnect(url)
            }

            ws.onerror = (e) => {
                console.error('[ws] error', e)
            }
        } catch (e) {
            console.error('[ws] connect failed', e)
            scheduleReconnect(url)
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [wsUrl])

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

    function startHeartbeat() {
        stopHeartbeat()
        heartbeatRef.current.missed = 0
        heartbeatRef.current.timer = setInterval(() => {
            const ws = wsRef.current
            if (!ws || ws.readyState !== WebSocket.OPEN) return
            try {
                ws.send(JSON.stringify({ type: 'ping' }))
                heartbeatRef.current.missed += 1
                if (heartbeatRef.current.missed > 2) {
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

    function scheduleReconnect(url = wsUrl) {
        reconnectAttempts.current = Math.min(10, reconnectAttempts.current + 1)
        const attempt = reconnectAttempts.current
        const delay = Math.min(30000, 200 * 2 ** attempt)
        setTimeout(() => connectWs(url), delay)
    }

    // Flush bufferRef into the current pending assistant message
    function flushBufferToMessage() {
        const text = bufferRef.current
        if (!text) return
        bufferRef.current = ''
        const aid = pendingAssistantId.current
        if (!aid) {
            const newId = NEXT_ID()
            pendingAssistantId.current = newId
            setMessages(prev => [...prev, { id: newId, role: 'assistant', text }])
            return
        }
        setMessages(prev => prev.map(m => m.id === aid ? { ...m, text: m.text + text } : m))
    }

    // start a periodic flush when streaming
    function ensureFlushTimer() {
        if (flushTimerRef.current) return
        flushTimerRef.current = setInterval(() => {
            flushBufferToMessage()
        }, 80)
    }

    function clearFlushTimer() {
        if (flushTimerRef.current) {
            clearInterval(flushTimerRef.current)
            flushTimerRef.current = null
        }
    }

    // unify incoming payload to a text delta
    function extractDeltaText(payload) {
        if (!payload || typeof payload !== 'object') return ''
        const candidates = [
            payload.text,
            payload.response,
            payload.response_text,
            payload.output,
            payload.content
        ]
        for (const v of candidates) {
            if (typeof v === 'string' && v.length > 0) return v
        }
        if (typeof payload.thinking === 'string' && payload.thinking.trim() !== '') {
            return ''
        }
        return ''
    }

    function handleWsPayload(payload) {
        if (!payload || typeof payload !== 'object') return
        if (payload.type === 'delta') {
            const delta = payload.text || ''
            bufferRef.current += delta
            ensureFlushTimer()
            return
        }

        const text = extractDeltaText(payload)
        if (text) {
            bufferRef.current += text
            ensureFlushTimer()
            if (payload.done === true) {
                flushBufferToMessage()
                pendingAssistantId.current = null
                setIsLoading(false)
                clearFlushTimer()
            }
            return
        }

        if (payload.type === 'done' || payload.done === true) {
            flushBufferToMessage()
            pendingAssistantId.current = null
            setIsLoading(false)
            clearFlushTimer()
            return
        }

        if (payload.type === 'error') {
            const errText = payload.error || '伺服器錯誤'
            const aid = pendingAssistantId.current
            if (aid) {
                setMessages(prev => prev.map(m => m.id === aid ? { ...m, text: errText } : m))
            } else {
                setMessages(prev => [...prev, { id: NEXT_ID(), role: 'assistant', text: errText }])
            }
            pendingAssistantId.current = null
            setIsLoading(false)
            clearFlushTimer()
            return
        }

        console.warn('[ws] unknown payload', payload)
    }

    async function sendMessage() {
        const trimmed = input.trim()
        if (!trimmed) return
        if (pendingAssistantId.current) {
            setMessages(prev => [...prev, { id: NEXT_ID(), role: 'assistant', text: '請等待前一則回覆完畢' }])
            return
        }

        const userMsg = { id: NEXT_ID(), role: 'user', text: trimmed }
        setMessages(prev => [...prev, userMsg])
        setInput('')
        setIsLoading(true)

        const assistantId = NEXT_ID()
        pendingAssistantId.current = assistantId
        setMessages(prev => [...prev, { id: assistantId, role: 'assistant', text: '' }])

        try {
            if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
                connectWs(wsUrl)
                await waitForWsOpen(wsRef.current, 5000)
            }
        } catch (e) {
            // Connection error already handled by UI state
            console.error('WebSocket connection error:', e);
            setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, text: '無法連線，請稍後再試。' } : m))
            pendingAssistantId.current = null
            setIsLoading(false)
            return
        }

        try {
            const ws = wsRef.current
            const payload = {
                model: 'gpt-oss:20b',
                messages: [{ role: 'user', content: trimmed }]
            }
            ws.send(JSON.stringify(payload))
        } catch (err) {
            // Send error already handled by UI state
            console.error('WebSocket send error:', err);
            setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, text: '送出失敗，請稍後再試。' } : m))
            pendingAssistantId.current = null
            setIsLoading(false)
            clearFlushTimer()
        }
    }

    // connect on mount; cleanup on unmount
    useEffect(() => {
        connectWs(wsUrl)
        return () => {
            try { 
                if (wsRef.current) wsRef.current.close() 
            } catch(e) {
                // WebSocket cleanup error - can be safely ignored during unmount
                console.debug('WebSocket cleanup error:', e);
            }
            stopHeartbeat()
            clearFlushTimer()
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
                                        {msg.role === 'assistant' ? (
                                            <MarkdownViewer source={msg.text} />
                                        ) : (
                                            msg.text
                                        )}
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
          <textarea
              className="input"
              value={input}
              onChange={e => setInput(e.target.value)}
              placeholder="請輸入您的問題..."
              disabled={isLoading}
              aria-label="輸入訊息"
              rows={3}
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
