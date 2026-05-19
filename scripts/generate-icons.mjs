/**
 * generate-icons.mjs — 从 icon.svg 生成各平台所需图标
 *
 * 用法: node scripts/generate-icons.mjs
 *
 * 输出:
 *   assets/icon.png     256x256 (源 PNG)
 *   assets/icon.ico     256x256 (Windows)
 *   assets/icon.icns    256x256 (macOS, 占位)
 *   assets/tray-icon.png  32x32  (托盘)
 *
 * 依赖: npm install sharp  (纯 JS，无需原生编译)
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const assetsDir = join(__dirname, '..', 'assets')

async function main() {
  let sharp
  try {
    sharp = (await import('sharp')).default
  } catch {
    console.error(' 请先安装 sharp: npm install -D sharp')
    process.exit(1)
  }

  const svgBuffer = readFileSync(join(assetsDir, 'icon.svg'))

  // 生成 256×256 PNG
  const png256 = await sharp(svgBuffer).resize(256, 256).png().toBuffer()
  writeFileSync(join(assetsDir, 'icon.png'), png256)
  console.log('✅ icon.png (256×256)')

  // 生成 32×32 托盘图标
  const png32 = await sharp(svgBuffer).resize(32, 32).png().toBuffer()
  writeFileSync(join(assetsDir, 'tray-icon.png'), png32)
  console.log('✅ tray-icon.png (32×32)')

  // 生成 16×16 小图标
  const png16 = await sharp(svgBuffer).resize(16, 16).png().toBuffer()
  writeFileSync(join(assetsDir, 'tray-icon@16.png'), png16)
  console.log('✅ tray-icon@16.png (16×16)')

  // Windows ICO — 简单的 ICO 封装 (256×256 PNG inside)
  const ico = createSimpleICO(png256)
  writeFileSync(join(assetsDir, 'icon.ico'), ico)
  console.log('✅ icon.ico (256×256)')

  // macOS ICNS — 占位（需专业工具生成，此处用 PNG 替代）
  // electron-builder 可接受 PNG 作为 icns 在开发阶段
  writeFileSync(join(assetsDir, 'icon.icns'), png256)
  console.log('⚠ icon.icns (PNG 占位，正式发布请使用 iconutil 生成)')

  console.log('\n🎉 所有图标生成完毕！')
}

/**
 * 生成简易 ICO 文件（将 PNG 嵌入 ICO 容器）
 * ICO 格式: https://en.wikipedia.org/wiki/ICO_(file_format)
 */
function createSimpleICO(pngBuffer) {
  const imageSize = pngBuffer.length
  const headerSize = 6
  const entrySize = 16
  const totalSize = headerSize + entrySize + imageSize

  const buf = Buffer.alloc(totalSize)

  // ICO Header
  buf.writeUInt16LE(0, 0)  // reserved
  buf.writeUInt16LE(1, 2)  // ICO type
  buf.writeUInt16LE(1, 4)  // image count

  // ICO Entry
  const width = Math.min(pngBuffer.readUInt32BE(16), 256) // extract width from PNG IHDR
  buf.writeUInt8(width >= 256 ? 0 : width, 6)  // width
  buf.writeUInt8(width >= 256 ? 0 : width, 7)  // height
  buf.writeUInt8(0, 8)    // palette
  buf.writeUInt8(0, 9)    // reserved
  buf.writeUInt16LE(1, 10) // planes
  buf.writeUInt16LE(32, 12) // bpp
  buf.writeUInt32LE(imageSize, 14) // image size
  buf.writeUInt32LE(headerSize + entrySize, 18) // offset

  // PNG data
  pngBuffer.copy(buf, headerSize + entrySize)

  return buf
}

main().catch(err => {
  console.error(' 图标生成失败:', err)
  process.exit(1)
})
