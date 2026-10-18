import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path/posix'
import { StringDecoder } from 'node:string_decoder'
import { setTimeout as delay } from 'node:timers/promises'
import { Sandbox, NotFoundError } from '@lizard-build/sdk'
import { createExecBackedGit, UnsupportedCapabilityError } from '@tanstack/ai-sandbox'
import type {
  SandboxCapabilities, SandboxCreateInput, SandboxHandle, SandboxProvider,
  SandboxFsStat, ProcessOptions, SpawnHandle, ExecResult,
} from '@tanstack/ai-sandbox'

const WORKER = readFileSync(new URL('./worker.py', import.meta.url), 'utf8')
const FILE_LIMIT = 4 * 1024 * 1024
const OUTPUT_LIMIT = 10 * 1024 * 1024
const CAPS: Readonly<SandboxCapabilities> = Object.freeze({
  fs: true, exec: true, env: true, ports: true,
  backgroundProcesses: true, writableStdin: false, killableProcesses: true,
  snapshots: false, networkPolicy: false, durableFilesystem: false, fork: false,
})

export interface LizardSandboxConfig {
  /** Falls back to LIZARD_API_KEY. Never passed into the guest. */
  apiKey?: string
  /** Falls back to LIZARD_PROJECT_ID. */
  projectId?: string
  size?: 'small' | 'medium' | 'large'
  /** Sandbox lifetime; default 30 minutes. */
  lifetimeMs?: number
  /** Deadline per foreground/background command; default 10 minutes. */
  commandTimeoutMs?: number
  /** Absolute guest directory backing /workspace; default /workspace. */
  workdir?: string
}

function validEnv(env: Record<string, string>): void {
  for (const [key, value] of Object.entries(env)) {
    if (!key || key.includes('=') || key.includes('\0') || typeof value !== 'string' || value.includes('\0')) {
      throw new TypeError('Environment keys and values must be valid strings.')
    }
  }
}

/** Single-consumer output stream. A process's output cap bounds its queue. */
class Output implements AsyncIterable<string> {
  private chunks: string[] = []
  private ended = false
  private error: unknown
  private wake?: () => void
  push(chunk: string): void {
    if (chunk) this.chunks.push(chunk)
    this.wake?.()
  }
  end(error?: unknown): void {
    this.ended = true
    this.error = error
    this.wake?.()
  }
  async *[Symbol.asyncIterator](): AsyncGenerator<string> {
    for (;;) {
      while (this.chunks.length) yield this.chunks.shift()!
      if (this.ended) {
        if (this.error !== undefined) throw this.error
        return
      }
      await new Promise<void>((resolve) => { this.wake = resolve })
    }
  }
}

type Poll = {
  stdout: string; stderr: string; done: boolean
  result: { exitCode?: number; error?: string } | null
}

class LizardHandle implements SandboxHandle {
  readonly provider = 'lizard'
  readonly capabilities = CAPS
  readonly git
  private readonly root = '/tmp/tanstack-ai-lizard-' + randomUUID()
  private readonly helper = this.root + '/worker.py'

  constructor(
    private readonly sandbox: Sandbox,
    readonly workspaceRoot: string,
    private readonly commandTimeoutMs: number,
  ) {
    this.git = createExecBackedGit(this.process, workspaceRoot)
  }

  get id(): string { return this.sandbox.sandboxId }

  private resolve(value: string): string {
    if (typeof value !== 'string' || value.includes('\0')) throw new TypeError('Invalid guest path.')
    if (value === '/workspace' || value.startsWith('/workspace/')) {
      return path.resolve(this.workspaceRoot, '.' + value.slice('/workspace'.length))
    }
    return path.resolve(this.workspaceRoot, value)
  }

  private async control<T>(action: string, value: string): Promise<T> {
    const result = await this.sandbox.process.exec(['python3', this.helper, action, value], { timeoutMs: 30_000 })
    if (result.exitCode !== 0 || result.truncated) throw new Error('The sandbox worker failed.')
    const reply: { value: T; error?: { errno: number; message: string } } = JSON.parse(result.stdout)
    if (reply.error) {
      const error = new Error(reply.error.message)
      Object.assign(error, { errno: reply.error.errno })
      throw error
    }
    return reply.value
  }

  async prepare(): Promise<void> {
    await this.sandbox.fs.write(this.helper, WORKER)
    await this.control('prepare', this.workspaceRoot)
  }

  readonly fs: SandboxHandle['fs'] = {
    read: async (file) => Buffer.from(await this.fs.readBytes(file)).toString('utf8'),
    readBytes: async (file) => {
      const target = this.resolve(file)
      await this.control('read_check', JSON.stringify({ path: target }))
      return this.sandbox.fs.readBytes(target)
    },
    write: async (file, data) => {
      const bytes = typeof data === 'string' ? Buffer.from(data) : data
      if (bytes.byteLength > FILE_LIMIT) throw new RangeError('File exceeds the 4 MiB write limit.')
      await this.sandbox.fs.write(this.resolve(file), bytes)
    },
    list: (file) => this.control('list', JSON.stringify({ path: this.resolve(file) })),
    mkdir: (file) => this.control('mkdir', JSON.stringify({ path: this.resolve(file) })),
    remove: async (file) => {
      const target = this.resolve(file)
      if (target === '/' || this.workspaceRoot === target || this.workspaceRoot.startsWith(target + '/')) {
        throw new Error('Cannot remove the workspace root or its ancestors.')
      }
      await this.control('remove', JSON.stringify({ path: target }))
    },
    rename: (from, to) => this.control('rename', JSON.stringify({ path: this.resolve(from), to: this.resolve(to) })),
    lstat: async (file) => (await this.control<SandboxFsStat | null>('lstat', JSON.stringify({ path: this.resolve(file) }))) ?? undefined,
    exists: async (file) => (await this.fs.lstat!(file)) !== undefined,
  }

  readonly env: SandboxHandle['env'] = {
    set: async (vars) => { validEnv(vars); await this.control('env', JSON.stringify(vars)) },
  }

  readonly ports: SandboxHandle['ports'] = {
    connect: async (port) => {
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new RangeError('Invalid port.')
      const exposed = await this.sandbox.exposePort(port)
      return { url: `https://${exposed.hostname}`, headers: { 'X-Lizard-Access-Token': exposed.accessToken } }
    },
  }

  readonly process: SandboxHandle['process'] = {
    exec: async (command, options): Promise<ExecResult> => {
      const child = await this.spawn(command, options)
      const collect = async (stream: AsyncIterable<string>) => {
        let output = ''
        for await (const chunk of stream) output += chunk
        return output
      }
      const [stdout, stderr, exitCode] = await Promise.all([collect(child.stdout), collect(child.stderr), child.wait()])
      return { stdout, stderr, exitCode }
    },
    spawn: (command, options) => this.spawn(command, options),
  }

  private async spawn(command: string, options: ProcessOptions = {}): Promise<SpawnHandle> {
    options.signal?.throwIfAborted()
    if (typeof command !== 'string' || command.includes('\0')) throw new TypeError('Invalid shell command.')
    validEnv(options.env ?? {})
    const job = this.root + '/job-' + randomUUID()
    await this.sandbox.fs.write(job + '/request.json', JSON.stringify({
      command, cwd: this.resolve(options.cwd ?? '/workspace'), env: options.env ?? {},
      timeout: this.commandTimeoutMs / 1000,
    }))
    options.signal?.throwIfAborted()
    // Await acquisition even on abort so we always have the PID to stop.
    const { pid } = await this.control<{ pid: number }>('start', job)
    const stdout = new Output()
    const stderr = new Output()
    const stop = async () => { await this.control('stop', String(pid)) }
    const abort = () => { void stop().catch(() => undefined) }
    options.signal?.addEventListener('abort', abort, { once: true })
    if (options.signal?.aborted) abort()
    const completion = (async () => {
      const outDecoder = new StringDecoder('utf8')
      const errDecoder = new StringDecoder('utf8')
      let outOffset = 0
      let errOffset = 0
      try {
        for (;;) {
          const response = await this.control<Poll>('poll', JSON.stringify({ job, stdout: outOffset, stderr: errOffset }))
          const out = Buffer.from(response.stdout, 'base64')
          const err = Buffer.from(response.stderr, 'base64')
          outOffset += out.length
          errOffset += err.length
          if (outOffset > OUTPUT_LIMIT || errOffset > OUTPUT_LIMIT) throw new RangeError('Command output exceeded 10 MiB per stream.')
          stdout.push(outDecoder.write(out))
          stderr.push(errDecoder.write(err))
          if (response.done) {
            stdout.push(outDecoder.end())
            stderr.push(errDecoder.end())
            options.signal?.throwIfAborted()
            if (response.result?.error) throw new Error(`Sandbox command ${response.result.error}.`)
            if (typeof response.result?.exitCode !== 'number') throw new Error('Missing command exit code.')
            stdout.end()
            stderr.end()
            return response.result.exitCode
          }
          await delay(100)
        }
      } catch (error) {
        await stop().catch(() => undefined)
        stdout.end(error)
        stderr.end(error)
        throw error
      } finally {
        options.signal?.removeEventListener('abort', abort)
        // This path is generated by this handle, never supplied by a caller.
        await this.sandbox.process.exec(['rm', '-rf', '--', job], { timeoutMs: 30_000 }).catch(() => undefined)
      }
    })()
    void completion.catch(() => undefined)
    return {
      pid, stdout, stderr, wait: () => completion,
      kill: async (signal = 'SIGTERM') => {
        if (signal !== 'SIGTERM' && signal !== 'SIGKILL' && signal !== 15 && signal !== 9) {
          throw new UnsupportedCapabilityError('lizard', 'process signal', 'Use SIGTERM or SIGKILL.')
        }
        await stop()
      },
      stdin: {
        write: async () => { throw new UnsupportedCapabilityError('lizard', 'writableStdin', 'Use a file and shell redirection.') },
        end: async () => {},
      },
    }
  }

  async destroy(): Promise<void> { await this.sandbox.kill() }
}

/** Create a TanStack AI provider using Lizard SDK on the server. */
export function lizardSandbox(config: LizardSandboxConfig = {}): SandboxProvider {
  const workdir = config.workdir ?? '/workspace'
  if (!path.isAbsolute(workdir) || workdir.includes('\0')) throw new TypeError('workdir must be an absolute guest path.')
  const lifetimeMs = config.lifetimeMs ?? 30 * 60_000
  const commandTimeoutMs = config.commandTimeoutMs ?? 10 * 60_000
  for (const value of [lifetimeMs, commandTimeoutMs]) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new RangeError('Timeouts must be positive integers.')
  }
  const connection = { apiKey: config.apiKey }
  const validateConnection = () => {
    if (new URL(process.env.LIZARD_API_URL ?? 'https://lizard.build').protocol !== 'https:') {
      throw new Error('LIZARD_API_URL must use HTTPS.')
    }
  }
  const handle = async (sandbox: Sandbox) => {
    const result = new LizardHandle(sandbox, workdir, commandTimeoutMs)
    await result.prepare()
    return result
  }
  return {
    name: 'lizard', capabilities: () => CAPS,
    create: async (input: SandboxCreateInput) => {
      input.signal?.throwIfAborted()
      validateConnection()
      if (input.policy?.capabilities?.network === 'deny') {
        throw new UnsupportedCapabilityError('lizard', 'networkPolicy', 'This provider cannot enforce blocked egress.')
      }
      validEnv(input.env ?? {})
      const projectId = config.projectId ?? process.env.LIZARD_PROJECT_ID
      if (!projectId) throw new Error('Set LIZARD_PROJECT_ID or pass projectId.')
      const sandbox = await Sandbox.create('interpreter', {
        ...connection, projectId, size: config.size ?? 'small', timeoutMs: lifetimeMs,
        envs: input.env, metadata: { integration: 'tanstack-ai', ...(input.id ? { 'tanstack-ai-key': input.id } : {}) },
      })
      try {
        input.signal?.throwIfAborted()
        const result = await handle(sandbox)
        input.signal?.throwIfAborted()
        return result
      } catch (error) {
        await sandbox.kill().catch(() => undefined)
        throw error
      }
    },
    resume: async (input) => {
      input.signal?.throwIfAborted()
      validateConnection()
      let sandbox: Sandbox
      try { sandbox = await Sandbox.connect(input.id, connection) }
      catch (error) { if (error instanceof NotFoundError) return null; throw error }
      const info = await sandbox.getInfo()
      if (info.status !== undefined && info.status !== 'running' && info.status !== 'ready') {
        throw new Error(`Sandbox is not running (${info.status}); resume it with Lizard SDK first.`)
      }
      input.signal?.throwIfAborted()
      return handle(sandbox)
    },
    destroy: async (input) => {
      input.signal?.throwIfAborted()
      validateConnection()
      try { const sandbox = await Sandbox.connect(input.id, connection); await sandbox.kill() }
      catch (error) { if (!(error instanceof NotFoundError)) throw error }
    },
  }
}
