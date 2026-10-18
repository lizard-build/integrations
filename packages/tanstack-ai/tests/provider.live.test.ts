import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { runJournalConformance } from '@tanstack/ai-sandbox/testkit'
import type { SandboxHandle } from '@tanstack/ai-sandbox'
import { lizardSandbox } from '../src/index.js'

const live = process.env.LIZARD_LIVE_TESTS === '1'
const provider = lizardSandbox({ lifetimeMs: 900_000 })

describe.skipIf(!live)('Lizard provider live', () => {
  let handle: SandboxHandle
  beforeAll(async () => { handle = await provider.create({ env: { INITIAL: 'yes' } }) })
  afterAll(async () => { await handle?.destroy() })

  it('shares commands and binary files with a reconnected handle', async () => {
    const bytes = new Uint8Array([0, 255, 10, 13, 128])
    await handle.fs.write('/workspace/nested/bytes.bin', bytes)
    expect(await handle.fs.readBytes('nested/bytes.bin')).toEqual(bytes)
    const attached = await provider.resume({ id: handle.id })
    expect(attached).not.toBeNull()
    expect(await attached!.fs.readBytes('nested/bytes.bin')).toEqual(bytes)
    const result = await handle.process.exec('printf "$INITIAL"; printf err >&2; exit 7')
    expect(result).toEqual({ stdout: 'yes', stderr: 'err', exitCode: 7 })
  })

  it('persists environment updates across reconnects', async () => {
    await handle.env.set({ UPDATED: 'new value' })
    const attached = await provider.resume({ id: handle.id })
    expect((await attached!.process.exec('printf "$UPDATED"')).stdout).toBe('new value')
  })

  it('handles directories, rename, lstat, and missing paths', async () => {
    await handle.fs.mkdir('tree/a')
    await handle.fs.write('tree/a/file', 'hello')
    await handle.fs.rename('tree/a/file', 'tree/a/moved')
    expect(await handle.fs.exists('tree/a/file')).toBe(false)
    expect((await handle.fs.lstat!('tree/a/moved'))?.type).toBe('file')
    expect((await handle.fs.list('tree/a'))[0]?.name).toBe('moved')
    await handle.fs.remove('tree')
    expect(await handle.fs.exists('tree')).toBe(false)
  })

  it('aborts a foreground process and prevents its later write', async () => {
    const controller = new AbortController()
    const task = handle.process.exec('echo $$ > pid; sleep 60; touch late-marker', { signal: controller.signal })
    void task.catch(() => undefined)
    for (let i = 0; i < 30 && !(await handle.fs.exists('pid')); i++) await new Promise((r) => setTimeout(r, 200))
    expect(await handle.fs.exists('pid')).toBe(true)
    const pid = (await handle.fs.read('pid')).trim()
    controller.abort(new Error('cancel test'))
    await expect(task).rejects.toThrow('cancel test')
    expect((await handle.process.exec(`kill -0 ${pid}`)).exitCode).not.toBe(0)
    expect(await handle.fs.exists('late-marker')).toBe(false)
  })

  it('exposes an authenticated HTTP port', async () => {
    const server = await handle.process.spawn('python3 -m http.server 8091')
    try {
      const channel = await handle.ports.connect(8091)
      const response = await fetch(channel.url, { headers: channel.headers })
      expect(response.ok).toBe(true)
    } finally { await server.kill(); await server.wait() }
  })
})

runJournalConformance({
  name: 'lizard',
  createHandle: async () => {
    const handle = await provider.create({})
    return { handle, dispose: () => handle.destroy() }
  },
  ...(!live ? { unsupported: { reason: 'Set LIZARD_LIVE_TESTS=1 and Lizard credentials.' } } : {}),
})
