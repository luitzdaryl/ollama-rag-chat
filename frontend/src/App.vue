<script setup>
import { ref, reactive, onMounted, watch, nextTick } from 'vue'
import { marked } from 'marked'
marked.setOptions({ breaks: true, gfm: true })

const models = ref([])
const selectedModel = ref('')
const userInput = ref('')
const messages = ref([])
const isStreaming = ref(false)
const composerEl = ref(null) // reference to the <textarea> DOM element

// New refsfor RAG 

const activeTab = ref('chat') // 'chat' | 'documents' | 'settings'
const documents = ref([])
const uploading = ref(false)
const uploadError = ref('')

const theme = ref(localStorage.getItem('theme') || 'dark')

//------------------------------------------------

// System Prompt Template for RAG — stored in localStorage so users can customize it and have it persist across sessions

const DEFAULT_PROMPT_TEMPLATE = `Answer the question using ONLY the context below. If the context doesn't contain the answer, say you don't know — do not make up information.

Context:
{context}

Question: {question}`

const promptTemplate = ref(localStorage.getItem('promptTemplate') || DEFAULT_PROMPT_TEMPLATE)

watch(promptTemplate, (val) => localStorage.setItem('promptTemplate', val))

function resetPromptTemplate() {
  promptTemplate.value = DEFAULT_PROMPT_TEMPLATE
}
//------------------------------------------------

function applyTheme() {
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('theme', theme.value)
}
function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
}
watch(theme, applyTheme)

const API_BASE = 'http://localhost:8000'

// onMounted is a Vue lifecycle hook that runs after the component is mounted to the DOM

onMounted(async () => {
  applyTheme()
  const res = await fetch(`${API_BASE}/api/models`)
  models.value = await res.json()
  if (models.value.length > 0) selectedModel.value = models.value[0]
  await loadDocuments()
})
//------------------------------------------------

// Grows the textarea as the user types, capped at 160px
function autoResizeComposer() {
  const el = composerEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

// Enter sends, Shift+Enter makes a new line
function handleComposerKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (!isStreaming.value && userInput.value.trim()) sendMessage()
  }
}


async function sendMessage() {
  const text = userInput.value.trim()
  if (!text || isStreaming.value) return

  messages.value.push({ role: 'user', content: text })
  userInput.value = ''
  await nextTick(autoResizeComposer)

  const assistantMessage = reactive({ role: 'assistant', content: '', sources: [] })
  messages.value.push(assistantMessage)

  isStreaming.value = true

  try {
    const response = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: selectedModel.value,
        messages: messages.value.slice(0, -1).map(m => ({ role: m.role, content: m.content })),
      }),
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()

    let buffer = ''
    let sourcesParsed = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      if (!sourcesParsed) {
        const newlineIndex = buffer.indexOf('\n')
        if (newlineIndex === -1) continue  // haven't received the full sources line yet — keep buffering

        const firstLine = buffer.slice(0, newlineIndex)
        if (firstLine.startsWith('__SOURCES__')) {
          try {
            assistantMessage.sources = JSON.parse(firstLine.slice('__SOURCES__'.length))
          } catch (e) {
            console.error('Failed to parse sources:', e)
          }
        }
        buffer = buffer.slice(newlineIndex + 1)
        sourcesParsed = true
      }

      assistantMessage.content += buffer
      buffer = ''
    }
  } catch (err) {
    assistantMessage.content = 'Error: could not reach the backend.'
    console.error(err)
  } finally {
    isStreaming.value = false
  }
}


// New function to handle file uploads for RAG

async function loadDocuments() {
  const res = await fetch(`${API_BASE}/api/documents`)
  const data = await res.json()
  documents.value = data.documents
}

async function handleFileUpload(event) {
  const file = event.target.files[0]
  if (!file) return

  uploading.value = true
  uploadError.value = ''
  try {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch(`${API_BASE}/api/documents/upload`, {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) {
      const err = await res.json()
      throw new Error(err.detail || 'Upload failed')
    }
    await loadDocuments()
  } catch (e) {
    uploadError.value = e.message
  } finally {
    uploading.value = false
    event.target.value = ''  // reset so re-uploading the same filename works
  }
}

async function deleteDocument(filename) {
  // encodeURIComponent matters here — filenames with spaces or special
  // characters would otherwise break the URL path
  await fetch(`${API_BASE}/api/documents/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  })
  await loadDocuments()
}

</script>

<template>
  <div class="app">
    <header class="app-header">
      <h1>Ollama Chat</h1>
      <button class="theme-toggle" @click="toggleTheme">
        {{ theme === 'dark' ? '☀️ Light' : '🌙 Dark' }}
      </button>
    </header>

    <select class="model-select" v-model="selectedModel">
      <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
    </select>

    <!-- Active Tab -->

    <div class="tabs">
      <button :class="{ active: activeTab === 'chat' }" @click="activeTab = 'chat'">💬 Chat</button>
      <button :class="{ active: activeTab === 'documents' }" @click="activeTab = 'documents'">📄 Documents</button>
      <button :class="{ active: activeTab === 'settings' }" @click="activeTab = 'settings'">⚙️ Settings</button>
    </div>

    <!-- New RAG documents panel -->

    <div v-if="activeTab === 'documents'" class="documents-panel">
     <div class="documents-panel">
        <div class="documents-header">
          <h3>📄 Documents</h3>
          <label class="upload-button">
            {{ uploading ? 'Uploading...' : '+ Upload' }}
            <input type="file" @change="handleFileUpload" :disabled="uploading" hidden />
          </label>
        </div>

        <p v-if="uploadError" class="upload-error">{{ uploadError }}</p>

        <ul v-if="documents.length" class="document-list">
          <li v-for="doc in documents" :key="doc" class="document-item">
            <span class="document-name">{{ doc }}</span>
            <button class="delete-button" @click="deleteDocument(doc)" title="Remove">✕</button>
          </li>
        </ul>
        <p v-else class="no-documents">No documents uploaded yet — chat will answer from the model's general knowledge only.</p>
      </div>
    </div>
    
    <template v-if="activeTab === 'chat'">
    <div class="chat-window">
      <div
        v-for="(msg, i) in messages"
        :key="i"
        class="message-wrap"
        :class="msg.role === 'user' ? 'message-wrap--user' : 'message-wrap--assistant'">
        <div class="bubble" :class="msg.role === 'user' ? 'bubble--user' : 'bubble--assistant'">
          <template v-if="msg.role === 'assistant'">
            <span v-html="marked.parse(msg.content)"></span>
          </template>
          <template v-else>
            {{ msg.content }}
          </template>
        </div>

        <!-- Show sources if available -->
        <details v-if="msg.sources && msg.sources.length" class="sources-expander">
          <summary>Sources ({{ msg.sources.length }})</summary>
          <ul class="sources-list">
            <li v-for="(src, i) in msg.sources" :key="i">
              {{ src.filename }} <span class="source-score">({{ src.score }})</span>
            </li>
          </ul>
        </details>
      </div>
    </div>

    <div class="composer">
      <textarea
        ref="composerEl"
        v-model="userInput"
        @keydown="handleComposerKey"
        @input="autoResizeComposer"
        placeholder="Ask something..."
        rows="1"
        class="composer-textarea"
        :disabled="isStreaming"></textarea>
      <button class="send-button" @click="sendMessage" :disabled="isStreaming || !userInput.trim()">
        <div class="spinner" v-if="isStreaming"></div>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
        </svg>
      </button>
    </div>
  </template>
    
  </div>
</template>

<style scoped>
.app { max-width: 1000px; margin: 40px auto; padding: 0 16px; }
.app-header { display: flex; align-items: center; justify-content: space-between; }

.theme-toggle {
  background: var(--color-surface-alt);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 6px 14px;
  cursor: pointer;
  font-size: 0.9rem;
}

.model-select {
  background: var(--color-surface);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 6px 10px;
  margin-bottom: 16px;
}

.chat-window {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
  min-height: 500px;
  max-height: 700px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Wrapper controls WHICH SIDE the bubble sits on */
.message-wrap { display: flex; flex-direction: column;}
.message-wrap--user { align-items: flex-end; }
.message-wrap--assistant { align-items: flex-start; }

/* Bubble itself doesn't care about side, just its own look */
.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 16px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble--user {
  background: var(--color-bubble-user);
  color: var(--color-bubble-user-text);
  border-bottom-right-radius: 4px; /* small "tail" corner, like iMessage/ChatGPT */
}

.bubble--assistant {
  background: var(--color-surface-alt);
  color: var(--color-text);
  border-bottom-left-radius: 4px;
}

/* Composer: rounded pill container, textarea + circular send button inside */
.composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 16px;
  padding: 8px 8px 8px 14px;
  margin-top: 16px;
}

.composer-textarea {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  color: var(--color-text);
  font-size: 0.95rem;
  font-family: inherit;
  line-height: 1.5;
  min-height: 24px;
  max-height: 160px;
  overflow-y: auto;
  padding: 6px 0;
}

.composer-textarea::placeholder { color: var(--color-text-secondary); }

.send-button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: var(--color-accent);
  color: var(--color-accent-text);
  border: none;
  cursor: pointer;
  flex-shrink: 0;
  transition: transform 0.1s ease;
}

.send-button:hover:not(:disabled) { transform: scale(1.05); }
.send-button:disabled { opacity: 0.5; cursor: not-allowed; }

.spinner {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: currentColor;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }


.bubble :deep(p) { margin: 0 0 8px; }
.bubble :deep(p:last-child) { margin-bottom: 0; }

.bubble :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85em;
  background: rgba(0, 0, 0, 0.25);
  padding: 2px 6px;
  border-radius: 5px;
}

.bubble :deep(pre) {
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 12px;
  overflow-x: auto;
  margin: 8px 0;
}

.bubble :deep(pre code) {
  background: none;
  padding: 0;
}

.bubble :deep(ul),
.bubble :deep(ol) {
  margin: 6px 0;
  padding-left: 20px;
}

.bubble :deep(strong) { font-weight: 700; }
.bubble :deep(a) { color: var(--color-accent); }

.bubble :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 0.85em;
}

.bubble :deep(th) {
  text-align: left;
  font-weight: 700;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  background: rgba(255, 154, 0, 0.12);
}

.bubble :deep(td) {
  padding: 8px 10px;
  border: 1px solid var(--color-border);
}

.bubble :deep(tbody tr:nth-child(even) td) {
  background: rgba(255, 255, 255, 0.03);
}


/* New RAG documents panel styles */

.documents-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 16px;
}

.documents-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.documents-header h3 {
  margin: 0;
  font-size: 0.95rem;
}

.upload-button {
  background: var(--color-accent);
  color: var(--color-accent-text);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
}

.upload-error {
  color: #f87171;
  font-size: 0.85rem;
  margin: 8px 0 0;
}

.document-list {
  list-style: none;
  padding: 0;
  margin: 10px 0 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.document-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-surface-alt);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 0.85rem;
}

.document-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.delete-button {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 0.9rem;
  padding: 0 4px;
}
.delete-button:hover { color: #f87171; }

.no-documents {
  color: var(--color-text-secondary);
  font-size: 0.85rem;
  margin: 10px 0 0;
}

.sources-expander {
  margin-top: 4px;
  max-width: 78%;
  font-size: 0.8rem;
  color: var(--color-text-secondary);
}
.sources-list { margin: 4px 0 0; padding-left: 18px; }
.source-score { opacity: 0.7; }

.tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.tabs button {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 0.85rem;
  cursor: pointer;
}
.tabs button.active {
  background: var(--color-accent);
  color: var(--color-accent-text);
  border-color: var(--color-accent);
}

</style>