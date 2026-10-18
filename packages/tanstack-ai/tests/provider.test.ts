import { beforeEach, expect, it, vi } from 'vitest'
const { create, connect, sdk } = vi.hoisted(() => ({
  create: vi.fn(), connect: vi.fn(),
  sdk: { sandboxId: 'box-123', kill: vi.fn(), getInfo: vi.fn(),
    process: { exec: vi.fn() }, fs: { write: vi.fn(), readBytes: vi.fn() }, exposePort: vi.fn() },
}))
vi.mock('@lizard-build/sdk', () => ({ Sandbox: { create, connect }, NotFoundError: class extends Error {} }))
import { NotFoundError } from '@lizard-build/sdk'
import { lizardSandbox } from '../src/index.js'

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubEnv('LIZARD_PROJECT_ID', 'project-123')
  vi.stubEnv('LIZARD_API_URL', 'https://lizard.build')
  create.mockResolvedValue(sdk)
  connect.mockResolvedValue(sdk)
  sdk.kill.mockResolvedValue(true)
  sdk.getInfo.mockResolvedValue({ status: 'running' })
  sdk.process.exec.mockImplementation(async (argv: string[]) => {
    const action = argv[2]
    const value = action === 'prepare' ? '/workspace' : action === 'start' ? { pid: 42 }
      : action === 'poll' ? { stdout: Buffer.from('out\n').toString('base64'), stderr: Buffer.from('err').toString('base64'), done: true, result: { exitCode: 7 } } : null
    return { stdout: JSON.stringify({ value }), stderr: '', exitCode: 0 }
  })
})

it('creates with the requested environment and bounded lifetime', async () => {
  const handle = await lizardSandbox({ apiKey: 'host-only' }).create({ env: { TEST: 'yes' }, id: 'thread-a' })
  expect(create).toHaveBeenCalledWith('interpreter', expect.objectContaining({ projectId: 'project-123', size: 'small', timeoutMs: 1800000, envs: { TEST: 'yes' } }))
  expect(handle.capabilities.writableStdin).toBe(false)
  expect(JSON.stringify(sdk.fs.write.mock.calls)).not.toContain('host-only')
})

it('rejects a denied network policy before creating resources', async () => {
  await expect(lizardSandbox().create({ policy: { capabilities: { network: 'deny' } } })).rejects.toThrow('networkPolicy')
  expect(create).not.toHaveBeenCalled()
})

it('kills a completed create when cancellation arrived during acquisition', async () => {
  const controller = new AbortController()
  create.mockImplementation(async () => { controller.abort(new Error('stop')); return sdk })
  await expect(lizardSandbox().create({ signal: controller.signal })).rejects.toThrow('stop')
  expect(sdk.kill).toHaveBeenCalledOnce()
})

it('kills a sandbox if preparation fails', async () => {
  sdk.fs.write.mockRejectedValue(new Error('write failed'))
  await expect(lizardSandbox().create({})).rejects.toThrow('write failed')
  expect(sdk.kill).toHaveBeenCalledOnce()
})

it('only treats confirmed absence as a missing sandbox', async () => {
  connect.mockRejectedValueOnce(new NotFoundError('gone'))
  expect(await lizardSandbox().resume({ id: 'gone' })).toBeNull()
  connect.mockRejectedValueOnce(new Error('rate limited'))
  await expect(lizardSandbox().resume({ id: 'box' })).rejects.toThrow('rate limited')
  expect(create).not.toHaveBeenCalled()
})

it('preserves output and nonzero exit codes', async () => {
  const handle = await lizardSandbox().create({})
  expect(await handle.process.exec('printf out; exit 7')).toEqual({ stdout: 'out\n', stderr: 'err', exitCode: 7 })
})

it('rejects unsupported stdin and invalid environment variables', async () => {
  const handle = await lizardSandbox().create({})
  const child = await handle.process.spawn('true')
  await expect(child.stdin.write('x')).rejects.toThrow('writableStdin')
  await child.wait()
  await expect(handle.env.set({ 'BAD=KEY': 'x' })).rejects.toThrow('Environment')
})

it('returns preview authentication as a header', async () => {
  sdk.exposePort.mockResolvedValue({ hostname: 'preview.example', accessToken: 'port-token' })
  const handle = await lizardSandbox().create({})
  expect(await handle.ports.connect(3000)).toEqual({ url: 'https://preview.example', headers: { 'X-Lizard-Access-Token': 'port-token' } })
})

it('rejects oversized files and removal of the workspace root', async () => {
  const handle = await lizardSandbox().create({})
  await expect(handle.fs.write('large', new Uint8Array(4 * 1024 * 1024 + 1))).rejects.toThrow('4 MiB')
  await expect(handle.fs.remove('/workspace')).rejects.toThrow('workspace root')
})

it('refuses insecure API endpoints before sending the key', async () => {
  vi.stubEnv('LIZARD_API_URL', 'http://example.com')
  await expect(lizardSandbox().create({})).rejects.toThrow('HTTPS')
  expect(create).not.toHaveBeenCalled()
})
