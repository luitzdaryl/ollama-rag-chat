import { marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js/lib/core'

import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import css from 'highlight.js/lib/languages/css'
import xml from 'highlight.js/lib/languages/xml'
import sql from 'highlight.js/lib/languages/sql'
import yaml from 'highlight.js/lib/languages/yaml'
import go from 'highlight.js/lib/languages/go'
import java from 'highlight.js/lib/languages/java'
import cpp from 'highlight.js/lib/languages/cpp'
import rust from 'highlight.js/lib/languages/rust'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import markdown from 'highlight.js/lib/languages/markdown'

import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github-dark.css'

// Importing highlight.js/lib/core + a handful of languages keeps the bundle
// far smaller than importing everything. Each language registers its own
// aliases automatically (js, py, sh, html, yml, ...).
const languages = {
  javascript, typescript, python, bash, json, css, xml, sql,
  yaml, go, java, cpp, rust, dockerfile, markdown,
}
for (const [name, def] of Object.entries(languages)) {
  hljs.registerLanguage(name, def)
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

marked.setOptions({ breaks: true, gfm: true })

marked.use(
  markedHighlight({
    emptyLangClass: 'hljs',
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value
      }
      // Unknown or missing language: no auto-detect (slow, and unreliable
      // on half-streamed code) — just show it escaped.
      return escapeHtml(code)
    },
  })
)

// throwOnError: false matters while streaming — a half-written formula
// renders as plain red text instead of throwing and breaking the message.
marked.use(markedKatex({ throwOnError: false }))

// The katex extension only understands $...$ and $$...$$, but many models
// emit \( ... \) and \[ ... \]. Convert them, skipping fenced code blocks.
function normalizeMathDelimiters(text) {
  return text
    .split(/(```[\s\S]*?```)/g)
    .map((part, i) => {
      if (i % 2 === 1) return part // odd indexes are fenced code — leave alone
      return part
        .replace(/\\\[([\s\S]*?)\\\]/g, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
        .replace(/\\\(([\s\S]*?)\\\)/g, (_, m) => `$${m.trim()}$`)
    })
    .join('')
}

export function renderMarkdown(text) {
  if (!text) return ''
  const html = marked.parse(normalizeMathDelimiters(text))
  // Wrap each code block and add a copy button inside the HTML itself.
  // Safe to do with plain replace: code content is already HTML-escaped,
  // so a literal "<pre>" can't appear inside it.
  return html
    .replace(/<pre>/g, '<div class="code-block"><button type="button" class="code-copy-btn">Copy</button><pre>')
    .replace(/<\/pre>/g, '</pre></div>')
}