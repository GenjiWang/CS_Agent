import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './style.css' // 如果 styles 在 src，或改成 '/styles.css' 指向根目錄

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <App />
    </React.StrictMode>
)