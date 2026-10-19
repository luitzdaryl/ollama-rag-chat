<script setup>
import { ref, reactive, computed, onMounted, watch, nextTick } from 'vue'
import { renderMarkdown } from './markdown.js'

const API_BASE = 'http://localhost:8000'
const META_MARKER = '__META__' // first line of every /api/chat stream (see backend)

// Custom directive: <input v-focus /> focuses the element when it appears
const vFocus = { mounted: (el) => el.focus() }

function loadJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback
  } catch {
    return fallback
  }
}

/* ───────── Core UI state ───────── */
const models = ref([])
const selectedModel = ref('')
const activeTab = ref('chat') // 'chat' | 'documents' | 'settings'
const userInput = ref('')
const isStreaming = ref(false)
const composerEl = ref(null)
const chatWindowEl = ref(null)

// Plain (non-reactive) handles for the request currently in flight
let abortController = null
let streamingChatId = null

watch(selectedModel, (v) => { if (v) localStorage.setItem('selectedModel', v) })

/* ───────── Confirm modal (used by every delete) ───────── */
const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  confirmLabel: 'Delete',
  resolve: null,
})
const cancelBtnEl = ref(null)

// Resolves true if confirmed, false if cancelled (Cancel, backdrop click, Escape)
function askConfirm({ title, message, confirmLabel = 'Delete' }) {
  if (confirmState.resolve) confirmState.resolve(false)
  return new Promise((resolve) => {
    confirmState.title = title
    confirmState.message = message
    confirmState.confirmLabel = confirmLabel
    confirmState.resolve = resolve
    confirmState.open = true
  })
}
function closeConfirm(result) {
  const resolve = confirmState.resolve
  confirmState.open = false
  confirmState.resolve = null
  resolve?.(result)
}
// Focus Cancel by default, so a stray Enter can never delete anything
watch(() => confirmState.open, async (open) => {
  if (open) {
    await nextTick()
    cancelBtnEl.value?.focus()
  }
})

/* ───────── Documents (RAG) ───────── */
const documents = ref([]) // [{ filename, chunks, file_size, uploaded_at }]
const uploading = ref(false)
const uploadError = ref('')

// Documents the user has UNticked. Storing exclusions (not inclusions) means
// newly uploaded files are searched by default.
const excludedDocs = ref(loadJson('excludedDocs', []))
watch(excludedDocs, (v) => localStorage.setItem('excludedDocs', JSON.stringify(v)), { deep: true })

const isDocIncluded = (name) => !excludedDocs.value.includes(name)
function toggleDoc(name) {
  excludedDocs.value = isDocIncluded(name)
    ? [...excludedDocs.value, name]
    : excludedDocs.value.filter((n) => n !== name)
}
function selectAllDocs() {
  excludedDocs.value = []
}
function selectNoDocs() {
  excludedDocs.value = documents.value.map((d) => d.filename)
}

const includedDocs = computed(() =>
  documents.value.map((d) => d.filename).filter(isDocIncluded)
)
// null = search everything; an array = search only these (backend treats [] as "skip retrieval")
const documentFilter = computed(() =>
  includedDocs.value.length === documents.value.length ? null : includedDocs.value
)

/* ───────── Settings (persisted) ───────── */
const useKnowledgeBase = ref(localStorage.getItem('useKnowledgeBase') !== 'false')
watch(useKnowledgeBase, (v) => localStorage.setItem('useKnowledgeBase', v))

const DEFAULT_INSTRUCTION = `Answer the question using ONLY the context below. If the context doesn't contain the answer, say you don't know — do not make up information.`
const promptInstruction = ref(localStorage.getItem('ragInstruction') || DEFAULT_INSTRUCTION)
watch(promptInstruction, (v) => localStorage.setItem('ragInstruction', v))
function resetPromptInstruction() {
  promptInstruction.value = DEFAULT_INSTRUCTION
}

const modeLabel = computed(() => {
  if (!useKnowledgeBase.value) return '💭 Model knowledge only'
  const total = documents.value.length
  const used = includedDocs.value.length
  if (!total) return '📚 Knowledge base is empty'
  if (!used) return '📚 No documents selected'
  if (used === total) return `📚 Using all ${total} document${total === 1 ? '' : 's'}`
  return `📚 Using ${used} of ${total} documents`
})

/* ───────── Theme ───────── */
const theme = ref(localStorage.getItem('theme') || 'dark')
function applyTheme() {
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('theme', theme.value)
}
function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
}
watch(theme, applyTheme)

/* ───────── Chats: state + persistence ───────── */
const CHATS_KEY = 'ragChat.chats'
const ACTIVE_KEY = 'ragChat.activeChatId'

function loadChats() {
  const saved = loadJson(CHATS_KEY, [])
  return Array.isArray(saved) ? saved : []
}

const storageFull = ref(false)
function saveChats() {
  try {
    localStorage.setItem(CHATS_KEY, JSON.stringify(chats.value))
    storageFull.value = false
  } catch (e) {
    console.warn('Could not save chats:', e)
    storageFull.value = true // surfaced in the sidebar so this never fails silently
  }
}

const chats = ref(loadChats())
const activeChatId = ref(localStorage.getItem(ACTIVE_KEY) || '')
const activeChat = computed(() => chats.value.find((c) => c.id === activeChatId.value))

// Save on any change, but not mid-stream: a deep watcher would re-serialize
// every chat on every token. We save explicitly when the stream finishes.
watch(chats, () => { if (!isStreaming.value) saveChats() }, { deep: true })
watch(activeChatId, (id) => localStorage.setItem(ACTIVE_KEY, id))

/* Sidebar search: matches chat titles AND message text */
const chatQuery = ref('')
const filteredChats = computed(() => {
  const q = chatQuery.value.trim().toLowerCase()
  if (!q) return chats.value
  return chats.value.filter(
    (c) =>
      c.title.toLowerCase().includes(q) ||
      c.messages.some((m) => (m.content || '').toLowerCase().includes(q))
  )
})

function createChat() {
  chatQuery.value = ''
  // Don't pile up empty chats; reuse the current one if it's still empty
  if (activeChat.value && activeChat.value.messages.length === 0) {
    activeTab.value = 'chat'
    return
  }
  const chat = { id: `chat_${Date.now()}`, title: 'New Chat', messages: [], createdAt: Date.now() }
  chats.value.unshift(chat)
  activeChatId.value = chat.id
  activeTab.value = 'chat'
}

function selectChat(id) {
  cancelEdit()
  activeChatId.value = id
  activeTab.value = 'chat'
}

async function deleteChat(id) {
  const chat = chats.value.find((c) => c.id === id)
  if (!chat) return

  const ok = await askConfirm({
    title: 'Delete this chat?',
    message: `"${chat.title}" and all of its messages will be permanently deleted. This can't be undone.`,
  })
  if (!ok) return

  if (isStreaming.value && streamingChatId === id) stopGeneration()
  const wasActive = id === activeChatId.value
  if (wasActive) cancelEdit() // otherwise the edit box would reappear on the next chat
  chats.value = chats.value.filter((c) => c.id !== id)
  if (!chats.value.length) createChat()
  else if (wasActive) activeChatId.value = chats.value[0].id
}

// First launch, or the saved active id no longer exists
if (!chats.value.length) createChat()
else if (!activeChat.value) activeChatId.value = chats.value[0].id

/* ───────── Rename ───────── */
const renamingChatId = ref(null)
const renameText = ref('')

function startRename(chat) {
  renamingChatId.value = chat.id
  renameText.value = chat.title
}
function confirmRename() {
  // Guard: Enter, then the input unmounting and firing blur, would run this twice
  if (renamingChatId.value === null) return
  const chat = chats.value.find((c) => c.id === renamingChatId.value)
  const name = renameText.value.trim()
  if (chat && name) chat.title = name
  renamingChatId.value = null
}
function cancelRename() {
  renamingChatId.value = null
}

/* ───────── Edit + resend a user message ───────── */
const editingIndex = ref(null)
const editingContent = ref('')

function startEdit(index) {
  editingIndex.value = index
  editingContent.value = activeChat.value.messages[index].content
}
function cancelEdit() {
  editingIndex.value = null
  editingContent.value = ''
}
async function saveEdit(index) {
  const text = editingContent.value.trim()
  if (!text || isStreaming.value) return
  const chat = activeChat.value
  chat.messages = chat.messages.slice(0, index) // drop the edited message and everything after it
  if (index === 0) chat.title = 'New Chat' // so the title regenerates from the new text
  cancelEdit()
  await sendMessage(text)
}

/* ───────── Scrolling ───────── */
// Measure "was the user at the bottom?" BEFORE Vue updates the DOM, so we
// don't yank the view down while someone is scrolled up reading.
async function scrollToBottom(force = false) {
  const el = chatWindowEl.value
  const wasNearBottom = el ? el.scrollHeight - el.scrollTop - el.clientHeight < 150 : true
  await nextTick()
  const target = chatWindowEl.value
  if (target && (force || wasNearBottom)) target.scrollTop = target.scrollHeight
}
watch(activeChatId, () => scrollToBottom(true))
watch(activeTab, (tab) => { if (tab === 'chat') scrollToBottom(true) })

/* ───────── Composer helpers ───────── */
function autoResizeComposer() {
  const el = composerEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

function handleComposerKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (!isStreaming.value && userInput.value.trim()) sendMessage()
  }
}

/* ───────── Sending, streaming, stopping, regenerating ───────── */
async function sendMessage(overrideText) {
  // overrideText is only a string when called from saveEdit; a click handler
  // would pass an event object, so check the type rather than truthiness.
  const text = (typeof overrideText === 'string' ? overrideText : userInput.value).trim()
  if (!text || isStreaming.value) return
  const chat = activeChat.value
  if (!chat) return

  chat.messages.push({ role: 'user', content: text, timestamp: Date.now() })
  if (chat.title === 'New Chat') {
    chat.title = text.length > 35 ? text.slice(0, 35) + '…' : text
  }
  saveChats() // persist the question right away, in case the tab closes mid-answer

  if (typeof overrideText !== 'string') {
    userInput.value = ''
    await nextTick(autoResizeComposer)
  }
  await generateAnswer(chat)
}

async function regenerate() {
  if (isStreaming.value) return
  const chat = activeChat.value
  const last = chat?.messages[chat.messages.length - 1]
  if (!last || last.role !== 'assistant') return
  chat.messages.pop() // drop the old answer; the question before it stays
  await generateAnswer(chat)
}

function stopGeneration() {
  abortController?.abort()
}

async function generateAnswer(chat) {
  const startedAt = Date.now()

  // History to send: skip error bubbles and empty messages
  const history = chat.messages
    .filter((m) => !m.isError && m.content)
    .map((m) => ({ role: m.role, content: m.content }))

  const assistantMessage = reactive({
    role: 'assistant',
    content: '',
    sources: [],
    searchQuery: null, // set when the backend rewrote a follow-up into a standalone query
    timestamp: null, // set when streaming finishes; the meta row appears then
    elapsed: '',
    modelName: selectedModel.value,
    isError: false,
    stopped: false,
  })
  chat.messages.push(assistantMessage)
  scrollToBottom(true)

  abortController = new AbortController()
  streamingChatId = chat.id
  isStreaming.value = true

  try {
    const response = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortController.signal,
      body: JSON.stringify({
        model: selectedModel.value,
        prompt_instruction: promptInstruction.value,
        use_knowledge_base: useKnowledgeBase.value,
        document_filter: documentFilter.value,
        messages: history,
      }),
    })
    if (!response.ok || !response.body) {
      const detail = await response.json().then((d) => d.detail).catch(() => null)
      throw new Error(detail || `Backend returned ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let metaParsed = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      if (!metaParsed) {
        // The marker itself might still be arriving in pieces
        if (buffer.length < META_MARKER.length && META_MARKER.startsWith(buffer)) continue
        if (buffer.startsWith(META_MARKER)) {
          const newlineIndex = buffer.indexOf('\n')
          if (newlineIndex === -1) continue // metadata line not complete yet
          try {
            const meta = JSON.parse(buffer.slice(META_MARKER.length, newlineIndex))
            assistantMessage.sources = meta.sources || []
            assistantMessage.searchQuery = meta.search_query || null
          } catch (e) {
            console.error('Failed to parse metadata:', e)
          }
          buffer = buffer.slice(newlineIndex + 1)
        }
        metaParsed = true
      }

      assistantMessage.content += buffer
      buffer = ''
      scrollToBottom()
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      assistantMessage.stopped = true // keep whatever text had already arrived
    } else {
      console.error(err)
      assistantMessage.isError = true
      assistantMessage.content =
        err instanceof TypeError ? 'Error: could not reach the backend.' : `Error: ${err.message}`
    }
  } finally {
    assistantMessage.timestamp = Date.now()
    assistantMessage.elapsed = ((Date.now() - startedAt) / 1000).toFixed(1) + 's'
    isStreaming.value = false
    abortController = null
    streamingChatId = null
    saveChats()
    await nextTick()
    if (activeTab.value === 'chat' && editingIndex.value === null) composerEl.value?.focus()
  }
}

/* ───────── Copy ───────── */
const copiedIndex = ref(null)
async function copyMessage(text, index) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    return
  }
  copiedIndex.value = index
  setTimeout(() => { if (copiedIndex.value === index) copiedIndex.value = null }, 1500)
}

// One delegated listener handles every code block's Copy button, including
// ones created later by v-html re-renders.
async function handleChatClick(e) {
  const btn = e.target.closest('.code-copy-btn')
  if (!btn) return
  const code = btn.parentElement.querySelector('pre code')
  try {
    await navigator.clipboard.writeText(code.textContent)
    btn.textContent = 'Copied!'
  } catch {
    btn.textContent = 'Failed'
  }
  setTimeout(() => { btn.textContent = 'Copy' }, 1500)
}

/* ───────── Documents ───────── */
async function loadDocuments() {
  try {
    const res = await fetch(`${API_BASE}/api/documents`)
    if (!res.ok) throw new Error(`status ${res.status}`)
    documents.value = (await res.json()).documents
    // Forget exclusions for files that no longer exist
    const names = documents.value.map((d) => d.filename)
    excludedDocs.value = excludedDocs.value.filter((n) => names.includes(n))
  } catch (e) {
    console.error('Could not load documents:', e)
  }
}

async function handleFileUpload(event) {
  const file = event.target.files[0]
  if (!file) return

  uploading.value = true
  uploadError.value = ''
  try {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch(`${API_BASE}/api/documents/upload`, { method: 'POST', body: formData })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Upload failed (${res.status})`)
    }
    await loadDocuments()
  } catch (e) {
    uploadError.value = e.message
  } finally {
    uploading.value = false
    event.target.value = '' // so re-uploading the same filename works
  }
}

async function deleteDocument(filename) {
  const ok = await askConfirm({
    title: 'Remove this document?',
    message: `"${filename}" will be removed from your knowledge base, and the assistant will no longer use it to answer questions. Your chats are not affected.`,
    confirmLabel: 'Remove',
  })
  if (!ok) return
  await fetch(`${API_BASE}/api/documents/${encodeURIComponent(filename)}`, { method: 'DELETE' })
  await loadDocuments()
}

/* ───────── Formatting helpers ───────── */
function fmtTime(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function fmtSize(bytes) {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`
}
function fmtDate(iso) {
  if (!iso) return 'date unknown'
  return new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
}

onMounted(async () => {
  applyTheme()
  try {
    const res = await fetch(`${API_BASE}/api/models`)
    models.value = await res.json()
    const saved = localStorage.getItem('selectedModel')
    if (models.value.length > 0) {
      selectedModel.value = models.value.includes(saved) ? saved : models.value[0]
    }
  } catch (e) {
    console.error('Could not load models:', e)
  }
  await loadDocuments()
  scrollToBottom(true)
})
</script>
<template>
  <div class="shell">
    <!-- ───── Sidebar: chat list ───── -->
    <aside class="sidebar">
      <button class="new-chat-btn" @click="createChat">＋ New chat</button>
 
      <div class="chat-list">
        <div
          v-for="chat in chats"
          :key="chat.id"
          class="chat-item"
          :class="{ active: chat.id === activeChatId }"
          @click="selectChat(chat.id)">
          <input
            v-if="renamingChatId === chat.id"
            v-model="renameText"
            v-focus
            class="rename-input"
            @click.stop
            @focus="$event.target.select()"
            @keydown.enter="confirmRename"
            @keydown.escape="cancelRename"
            @blur="confirmRename"/>
          <template v-else>
            <span class="chat-title" @dblclick.stop="startRename(chat)">{{ chat.title }}</span>
            <span class="chat-actions">
              <button title="Rename" @click.stop="startRename(chat)">✎</button>
              <button title="Delete" class="danger" @click.stop="deleteChat(chat.id)">✕</button>
            </span>
          </template>
        </div>
      </div>
    </aside>
 
    <!-- ───── Top bar (right column only): header + model select + tabs ───── -->
    <div class="topbar">
      <header class="app-header">
        <h1>Ollama Rag Chat</h1>
        <button class="theme-toggle" @click="toggleTheme">
          {{ theme === 'dark' ? '☀️ Light' : '🌙 Dark' }}
        </button>
      </header>
 
      <select class="model-select" v-model="selectedModel">
        <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
      </select>
 
      <div class="tabs">
        <button :class="{ active: activeTab === 'chat' }" @click="activeTab = 'chat'">💬 Chat</button>
        <button :class="{ active: activeTab === 'documents' }" @click="activeTab = 'documents'">📄 Documents</button>
        <button :class="{ active: activeTab === 'settings' }" @click="activeTab = 'settings'">⚙️ Settings</button>
      </div>
    </div>
 
    <!-- ───── Content (fills the remaining height) ───── -->
    <main class="content">
      <!-- Documents tab -->
      <div v-if="activeTab === 'documents'" class="documents-panel">
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
 
      <!-- Chat tab -->
      <template v-if="activeTab === 'chat' && activeChat">
        <div ref="chatWindowEl" class="chat-window" @click="handleChatClick">
          <p v-if="!activeChat.messages.length" class="empty-chat">
            Ask a question about your documents — or switch the knowledge base off in Settings to chat with the model directly.
          </p>
 
          <div
            v-for="(msg, i) in activeChat.messages"
            :key="i"
            class="message-wrap"
            :class="msg.role === 'user' ? 'message-wrap--user' : 'message-wrap--assistant'">
            <!-- Editing a user message -->
            <template v-if="editingIndex === i">
              <textarea
                v-model="editingContent"
                v-focus
                class="edit-box"
                rows="3"
                @keydown.escape="cancelEdit"
                @keydown.meta.enter="saveEdit(i)"
                @keydown.ctrl.enter="saveEdit(i)"></textarea>
              <div class="edit-actions">
                <button class="edit-save" @click="saveEdit(i)">Save &amp; send</button>
                <button class="edit-cancel" @click="cancelEdit">Cancel</button>
              </div>
            </template>
 
            <!-- Normal display -->
            <template v-else>
              <div class="bubble" :class="msg.role === 'user' ? 'bubble--user' : 'bubble--assistant'">
                <div v-if="msg.role === 'assistant'" v-html="renderMarkdown(msg.content)"></div>
                <template v-else>{{ msg.content }}</template>
              </div>
 
              <div v-if="msg.timestamp" class="message-meta">
                <span>{{ fmtTime(msg.timestamp) }}</span>
                <template v-if="msg.role === 'assistant' && msg.elapsed">
                  <span>·</span><span>{{ msg.elapsed }}</span>
                  <span>·</span><span>{{ msg.modelName }}</span>
                </template>
                <button
                  v-if="msg.role === 'user' && !isStreaming"
                  class="edit-msg-btn"
                  title="Edit and resend"
                  @click="startEdit(i)">✎ Edit</button>
              </div>
 
              <details v-if="msg.sources && msg.sources.length" class="sources-expander">
                <summary>Sources ({{ msg.sources.length }})</summary>
                <ul class="sources-list">
                  <li v-for="(src, si) in msg.sources" :key="si">
                    {{ src.filename }} <span class="source-score">({{ src.score }})</span>
                  </li>
                </ul>
              </details>
            </template>
          </div>
        </div>
 
        <div class="mode-indicator">
          {{ useKnowledgeBase ? '📚 Using your documents' : '💭 Model knowledge only' }}
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
          <button class="send-button" @click="sendMessage()" :disabled="isStreaming || !userInput.trim()">
            <div class="spinner" v-if="isStreaming"></div>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </div>
      </template>
 
      <!-- Settings tab -->
      <div v-if="activeTab === 'settings'" class="settings-panel">
        <div class="toggle-row">
          <div>
            <h3>📚 Use Knowledge Base</h3>
            <p class="settings-hint">When off, the assistant answers from its own knowledge only — no document retrieval.</p>
          </div>
          <label class="switch">
            <input type="checkbox" v-model="useKnowledgeBase"/>
            <span class="switch-slider"></span>
          </label>
        </div>
 
        <h3>⚙️ RAG Instructions</h3>
        <p class="settings-hint">Customize how the assistant should behave when answering from your uploaded documents.</p>
        <textarea v-model="promptInstruction" class="prompt-textarea" rows="6"></textarea>
        <button class="reset-button" @click="resetPromptInstruction">Reset to default</button>
      </div>
    </main>

    <!-- ───── Confirm modal (used by every delete) ───── -->
    <Teleport to="body">
      <div
        v-if="confirmState.open"
        class="modal-backdrop"
        tabindex="-1"
        @click.self="closeConfirm(false)"
        @keydown.esc="closeConfirm(false)">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <h3 id="confirm-title">{{ confirmState.title }}</h3>
          <p>{{ confirmState.message }}</p>
          <div class="modal-actions">
            <button ref="cancelBtnEl" class="modal-cancel" @click="closeConfirm(false)">Cancel</button>
            <button class="modal-danger" @click="closeConfirm(true)">{{ confirmState.confirmLabel }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* ───────────────────────────────────────────────
   Layout: locked to the viewport, only the chat
   window scrolls. Sidebar sits in the same row as
   the chat area, so its top edge lines up with it.
   ─────────────────────────────────────────────── */
 
/* Non-scoped rules that stop the page itself from scrolling */
:global(html),
:global(body),
:global(#app) {
  margin: 0;
  padding: 0;
  height: 100%;
  max-width: none;   /* in case a default Vue template sets one on #app */
  overflow: hidden;
}
 
.shell {
  box-sizing: border-box;
  height: 100dvh;
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px 16px;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr); /* minmax(0,…) stops wide code/tables from blowing out the grid */
  grid-template-rows: auto minmax(0, 1fr);
  column-gap: 24px;
}
 
.topbar  { grid-column: 2; grid-row: 1; min-width: 0; }
.sidebar { grid-column: 1; grid-row: 2; }
.content {
  grid-column: 2;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
 
/* ───────── Top bar ───────── */
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
 
/* ───────── Sidebar ───────── */
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 10px;
}
 
/* Gentle "New chat": outlined, no solid fill */
.new-chat-btn {
  background: transparent;
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 0.85rem;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.new-chat-btn:hover {
  background: var(--color-surface-alt);
  border-color: var(--color-accent);
}
 
.chat-list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}
 
.chat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
}
.chat-item:hover { background: var(--color-surface-alt); }
.chat-item.active { background: var(--color-surface-alt); border-color: var(--color-accent); }
 
.chat-title {
  flex: 1;
  min-width: 0;
  font-size: 0.85rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
 
.chat-actions { display: none; gap: 2px; }
.chat-item:hover .chat-actions { display: flex; }
.chat-actions button {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 0.8rem;
  padding: 2px 5px;
  border-radius: 5px;
}
.chat-actions button:hover { background: var(--color-border); color: var(--color-text); }
.chat-actions button.danger:hover { color: #f87171; }
 
.rename-input {
  flex: 1;
  min-width: 0;
  background: var(--color-surface);
  color: var(--color-text);
  border: 1px solid var(--color-accent);
  border-radius: 6px;
  padding: 3px 6px;
  font-size: 0.85rem;
  font-family: inherit;
  outline: none;
}
 
/* ───────── Chat window ───────── */
.chat-window {
  flex: 1 1 auto;
  min-height: 0;            /* lets it shrink so the page never grows */
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
 
.empty-chat {
  margin: auto;
  max-width: 360px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 0.9rem;
}
 
/* Wrapper controls WHICH SIDE the bubble sits on */
.message-wrap { display: flex; flex-direction: column; }
.message-wrap--user { align-items: flex-end; }
.message-wrap--assistant { align-items: flex-start; }
 
/* Bubble itself doesn't care about side, just its own look */
.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 16px;
  line-height: 1.5;
  word-break: break-word;
}
 
.bubble--user {
  background: var(--color-bubble-user);
  color: var(--color-bubble-user-text);
  border-bottom-right-radius: 4px; /* small "tail" corner, like iMessage/ChatGPT */
  white-space: pre-wrap;
}
 
.bubble--assistant {
  background: var(--color-surface-alt);
  color: var(--color-text);
  border-bottom-left-radius: 4px;
  white-space: normal;
}
 
/* ───────── Rendered markdown inside bubbles ───────── */
.bubble :deep(p) { margin: 0 0 8px; }
.bubble :deep(p:last-child) { margin-bottom: 0; }
 
.bubble :deep(code) {
  background: rgba(128, 128, 128, 0.2);
}
 
.bubble :deep(pre) {
  background: #161616;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 12px;
  overflow-x: auto;
  margin: 0;
}
.bubble :deep(.code-block) { position: relative; margin: 8px 0; }
.bubble :deep(.code-copy-btn) {
  position: absolute;
  top: 6px;
  right: 6px;
  background: rgba(255, 255, 255, 0.08);
  color: #c9d1d9;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 0.7rem;
  cursor: pointer;
  opacity: 0.6;
}
.bubble :deep(.code-block:hover .code-copy-btn) { opacity: 1; }
 
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
  display: block;
  overflow-x: auto;
  max-width: 100%;
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
  background: rgba(128, 128, 128, 0.08);
}
 
.bubble :deep(h1),
.bubble :deep(h2),
.bubble :deep(h3) { margin: 12px 0 6px; font-weight: 700; line-height: 1.3; }
.bubble :deep(h1) { font-size: 1.15rem; }
.bubble :deep(h2) { font-size: 1.05rem; }
.bubble :deep(h3) { font-size: 0.98rem; }
.bubble :deep(blockquote) {
  border-left: 3px solid var(--color-accent);
  margin: 8px 0;
  padding: 2px 12px;
  color: var(--color-text-secondary);
}
.bubble :deep(hr) { border: none; border-top: 1px solid var(--color-border); margin: 12px 0; }
.bubble :deep(li) { margin: 3px 0; }
.bubble :deep(.katex-display) { overflow-x: auto; overflow-y: hidden; margin: 0.8em 0; }
 
/* ───────── Message meta + sources ───────── */
.message-meta {
  display: flex;
  gap: 6px;
  font-size: 0.7rem;
  color: var(--color-text-secondary);
  margin-top: 3px;
  padding: 0 4px;
}
 
.sources-expander {
  margin-top: 4px;
  max-width: 78%;
  font-size: 0.8rem;
  color: var(--color-text-secondary);
}
.sources-list { margin: 4px 0 0; padding-left: 18px; }
.source-score { opacity: 0.7; }
 
/* ───────── Edit + resend ───────── */
.edit-msg-btn {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 0.7rem;
  cursor: pointer;
  padding: 0;
  opacity: 0;
  transition: opacity 0.15s;
}
.message-wrap:hover .edit-msg-btn { opacity: 1; }
.edit-msg-btn:hover { color: var(--color-accent); }
 
.edit-box {
  width: 78%;
  align-self: flex-end;
  background: var(--color-surface-alt);
  color: var(--color-text);
  border: 1px solid var(--color-accent);
  border-radius: 12px;
  padding: 10px 12px;
  font-family: inherit;
  font-size: 0.95rem;
  line-height: 1.5;
  resize: vertical;
  outline: none;
  box-sizing: border-box;
}
.edit-actions { display: flex; gap: 8px; align-self: flex-end; margin-top: 6px; }
.edit-save,
.edit-cancel {
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 0.8rem;
  cursor: pointer;
  border: 1px solid var(--color-border);
  background: var(--color-surface-alt);
  color: var(--color-text);
}
.edit-save {
  background: var(--color-accent);
  color: var(--color-accent-text);
  border-color: var(--color-accent);
  font-weight: 600;
}
 
/* ───────── Mode indicator + composer (pinned below the chat window) ───────── */
.mode-indicator {
  flex-shrink: 0;
  font-size: 0.78rem;
  color: var(--color-text-secondary);
  padding: 0 4px;
  margin-top: 8px;
}
 
/* Rounded pill container, textarea + circular send button inside */
.composer {
  flex-shrink: 0;
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
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: currentColor;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
 
/* ───────── Documents panel ───────── */
.documents-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 16px;
  overflow-y: auto;   /* scrolls internally instead of growing the page */
  min-height: 0;
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
 
/* ───────── Settings panel ───────── */
.settings-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px 20px;
  overflow-y: auto;   /* scrolls internally instead of growing the page */
  min-height: 0;
}
 
.settings-panel h3 {
  margin: 0 0 8px;
  font-size: 1rem;
}
 
.settings-hint {
  color: var(--color-text-secondary);
  font-size: 0.85rem;
  margin: 0 0 12px;
}
 
.prompt-textarea {
  width: 100%;
  background: var(--color-surface-alt);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 10px 12px;
  font-family: inherit;
  font-size: 0.9rem;
  line-height: 1.5;
  resize: vertical;
  box-sizing: border-box;
}
 
.prompt-textarea:focus {
  outline: none;
  border-color: var(--color-accent);
}
 
.reset-button {
  margin-top: 10px;
  background: var(--color-surface-alt);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 0.85rem;
  cursor: pointer;
}
.reset-button:hover { border-color: var(--color-accent); }
 
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--color-border);
}
 
.switch {
  position: relative;
  display: inline-block;
  width: 42px;
  height: 24px;
  flex-shrink: 0;
}
.switch input { opacity: 0; width: 0; height: 0; }
.switch-slider {
  position: absolute;
  cursor: pointer;
  inset: 0;
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: 24px;
  transition: background 0.2s;
}
.switch-slider::before {
  content: "";
  position: absolute;
  height: 16px;
  width: 16px;
  left: 3px;
  bottom: 2px;
  background: var(--color-text-secondary);
  border-radius: 50%;
  transition: transform 0.2s, background 0.2s;
}
.switch input:checked + .switch-slider { background: var(--color-accent); border-color: var(--color-accent); }
.switch input:checked + .switch-slider::before { transform: translateX(18px); background: white; }
 
/* ───────── Narrow screens: stack everything ───────── */
@media (max-width: 860px) {
  .shell {
    grid-template-columns: 1fr;
    grid-template-rows: auto auto minmax(0, 1fr);
  }
  .topbar, .sidebar, .content { grid-column: 1; }
  .topbar  { grid-row: 1; }
  .sidebar { grid-row: 2; max-height: 140px; margin-bottom: 12px; }
  .content { grid-row: 3; }
}


/* ───────── Confirm modal ───────── */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(2px);
  outline: none;
}
.modal {
  width: 100%;
  max-width: 400px;
  background: var(--color-surface);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 20px 22px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
}
.modal h3 { margin: 0 0 8px; font-size: 1.05rem; }
.modal p {
  margin: 0 0 18px;
  font-size: 0.9rem;
  line-height: 1.5;
  color: var(--color-text-secondary);
  word-break: break-word;
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
.modal-cancel,
.modal-danger {
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 0.85rem;
  cursor: pointer;
  border: 1px solid var(--color-border);
}
.modal-cancel { background: var(--color-surface-alt); color: var(--color-text); }
.modal-cancel:hover { border-color: var(--color-accent); }
.modal-danger { background: #dc2626; border-color: #dc2626; color: #fff; font-weight: 600; }
.modal-danger:hover { background: #b91c1c; border-color: #b91c1c; }

</style>