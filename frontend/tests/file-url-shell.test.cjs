/**
 * Static half of the file:// smoke gate. The browser half is run with the
 * local Playwright/Electron runner; this check fails fast if Vite ever emits
 * root-relative assets or removes the CSP-safe fallback shell.
 */
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

const root = path.resolve(__dirname, '..')
const source = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
const builtPath = path.join(root, 'dist', 'index.html')
const html = fs.existsSync(builtPath) ? fs.readFileSync(builtPath, 'utf8') : source

assert.match(source, /connect-src[^>]*http:\/\/127\.0\.0\.1:\*/) 
assert.match(source, /script-src\s+'self'/)
assert.match(source, /正在连接/)
assert.match(source, /href="\.\/index\.html"/)
assert.doesNotMatch(source, /<script\b[^>]*>[^<]+<\/script>/i,
  'fallback must not depend on an inline script blocked by the CSP')
assert.match(html, /<script[^>]+src="\.\/assets\//,
  'built entry script must be relative for Electron file://')
assert.doesNotMatch(html, /(?:src|href)="\/(?!\/)/,
  'built runtime assets must not be root-relative')
assert.match(html, /id="boot-shell"/)
console.log(`file:// shell contract: PASS (${fs.existsSync(builtPath) ? 'dist' : 'source'})`)
