import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles/index.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('Elementul #root lipseste din index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
