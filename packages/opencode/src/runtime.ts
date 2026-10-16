import { posix } from 'node:path';
import { Sandbox } from '@lizard-build/sdk';

export const MAX_TEXT_BYTES = 65536;
export const descriptions = {
  create: 'Create or show this OpenCode session’s sandbox in Lizard (lizard.build). It expires after 15 minutes by default. Files do not copy from the host.',
  exec: 'Run a shell command in this session’s remote sandbox. Creates a sandbox if needed. Cancellation or timeout destroys it and its files. Built-in OpenCode tools still run locally.',
  read: 'Read a UTF-8 text file in this session’s remote sandbox (up to 64 KiB). Relative paths start at /workspace.',
  write: 'Write a UTF-8 text file in this session’s remote sandbox (up to 64 KiB). Creates a sandbox if needed. Relative paths start at /workspace.',
  close: 'Destroy this session’s sandbox and its files. A later tool call can create a new sandbox.',
} as const;

export interface RemoteSandbox {
  sandboxId: string;
  kill(): Promise<boolean>;
  process: {
    exec(command: string, options: { timeoutMs: number; workdir: string }): Promise<{
      stdout: string; stderr: string; exitCode: number; truncated?: boolean;
    }>;
  };
  fs: {
    read(path: string): Promise<string>;
    write(path: string, content: string): Promise<unknown>;
    stat(path: string): Promise<{ size: number }>;
  };
}

export interface RuntimeOptions {
  lifetimeMs?: number;
  create?: (sessionID: string, lifetimeMs: number) => Promise<RemoteSandbox>;
}

function integer(value: number, name: string, min: number, max: number): number {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}.`);
  }
  return value;
}

export function optionsFrom(input: Record<string, unknown> = {}): RuntimeOptions {
  if (input.lifetimeMs === undefined) return {};
  if (typeof input.lifetimeMs !== 'number') throw new Error('lifetimeMs must be a number.');
  return { lifetimeMs: integer(input.lifetimeMs, 'lifetimeMs', 60000, 3600000) };
}

function remotePath(value: string): string {
  if (!value || value.includes('\0')) throw new Error('Provide a non-empty remote path without NUL bytes.');
  return posix.resolve('/workspace', value);
}

function clipped(text: string): { text: string; truncated: boolean } {
  const bytes = Buffer.from(text);
  if (bytes.length <= MAX_TEXT_BYTES) return { text, truncated: false };
  // Streaming decode omits an incomplete final UTF-8 character.
  return { text: new TextDecoder().decode(bytes.subarray(0, MAX_TEXT_BYTES), { stream: true }), truncated: true };
}

async function createSandbox(sessionID: string, lifetimeMs: number): Promise<RemoteSandbox> {
  const apiKey = process.env.LIZARD_API_KEY;
  const projectId = process.env.LIZARD_PROJECT_ID;
  if (!apiKey || !projectId) throw new Error('Set LIZARD_API_KEY and LIZARD_PROJECT_ID before using Lizard Sandboxes tools.');
  return Sandbox.create('interpreter', {
    apiKey, projectId, apiUrl: process.env.LIZARD_API_URL,
    size: 'small', timeoutMs: lifetimeMs,
    metadata: { integration: 'opencode', sessionId: sessionID },
  });
}

export class SandboxRuntime {
  private readonly lifetimeMs: number;
  private readonly factory: NonNullable<RuntimeOptions['create']>;
  private readonly sandboxes = new Map<string, { sandbox: RemoteSandbox; expiresAt: number }>();
  private readonly queues = new Map<string, Promise<unknown>>();
  private disposed = false;

  constructor(options: RuntimeOptions = {}) {
    this.lifetimeMs = integer(options.lifetimeMs ?? 900000, 'lifetimeMs', 60000, 3600000);
    this.factory = options.create ?? createSandbox;
  }

  private queue<T>(sessionID: string, action: () => Promise<T>): Promise<T> {
    if (!sessionID) return Promise.reject(new Error('OpenCode session ID is required.'));
    const previous = this.queues.get(sessionID) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(action);
    this.queues.set(sessionID, next);
    void next.finally(() => {
      if (this.queues.get(sessionID) === next) this.queues.delete(sessionID);
    }).catch(() => undefined);
    return next;
  }

  private async destroy(sessionID: string): Promise<boolean> {
    const entry = this.sandboxes.get(sessionID);
    if (!entry) return false;
    await entry.sandbox.kill();
    this.sandboxes.delete(sessionID);
    return true;
  }

  private async get(sessionID: string, signal: AbortSignal): Promise<RemoteSandbox> {
    signal.throwIfAborted();
    if (this.disposed) throw new Error('The plugin has stopped.');
    const current = this.sandboxes.get(sessionID);
    if (current) {
      if (Date.now() < current.expiresAt) return current.sandbox;
      await this.destroy(sessionID);
      throw new Error('The sandbox expired and its files are gone. Run the tool again to create a new sandbox.');
    }
    const started = Date.now();
    const sandbox = await this.factory(sessionID, this.lifetimeMs);
    this.sandboxes.set(sessionID, { sandbox, expiresAt: started + this.lifetimeMs });
    if (signal.aborted || this.disposed) {
      await this.destroy(sessionID);
      signal.throwIfAborted();
      throw new Error('The plugin stopped while the sandbox was being created.');
    }
    return sandbox;
  }

  private run<T>(sessionID: string, signal: AbortSignal, action: (sandbox: RemoteSandbox) => Promise<T>, timeoutMs = 120000, stopOnError = false): Promise<T> {
    return this.queue(sessionID, async () => {
      const sandbox = await this.get(sessionID, signal);
      let timer: ReturnType<typeof setTimeout> | undefined;
      let cancelled = false;
      let abort = () => {};
      const interrupted = new Promise<never>((_, reject) => {
        abort = () => {
          cancelled = true;
          reject(signal.reason ?? new Error('Tool call cancelled.'));
        };
        signal.addEventListener('abort', abort, { once: true });
        timer = setTimeout(() => {
          cancelled = true;
          reject(new Error('Tool call timed out; its sandbox was destroyed.'));
        }, timeoutMs);
      });
      try {
        signal.throwIfAborted();
        return await Promise.race([action(sandbox), interrupted]);
      } catch (error) {
        if (cancelled || signal.aborted || stopOnError) {
          try { await this.destroy(sessionID); }
          catch (cleanup) { throw new AggregateError([error, cleanup], 'The tool failed and sandbox cleanup failed. Retry lizard_sandbox_close; the lifetime limit still applies.'); }
        }
        throw error;
      } finally {
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
      }
    });
  }

  create(sessionID: string, signal: AbortSignal) {
    return this.run(sessionID, signal, async sandbox => ({ sandboxId: sandbox.sandboxId, workdir: '/workspace', lifetimeMs: this.lifetimeMs }));
  }

  exec(sessionID: string, signal: AbortSignal, args: { command: string; cwd?: string; timeoutMs?: number }) {
    if (!args.command.trim()) throw new Error('command must not be empty.');
    const timeoutMs = integer(args.timeoutMs ?? 60000, 'timeoutMs', 1, 120000);
    const workdir = remotePath(args.cwd ?? '/workspace');
    return this.run(sessionID, signal, async sandbox => {
      const result = await sandbox.process.exec(args.command, { timeoutMs, workdir });
      const stdout = clipped(result.stdout);
      const stderr = clipped(result.stderr);
      return { sandboxId: sandbox.sandboxId, exitCode: result.exitCode, stdout: stdout.text, stderr: stderr.text, truncated: !!result.truncated || stdout.truncated || stderr.truncated };
    }, timeoutMs, true);
  }

  read(sessionID: string, signal: AbortSignal, path: string) {
    const resolved = remotePath(path);
    return this.run(sessionID, signal, async sandbox => {
      const info = await sandbox.fs.stat(resolved);
      if (info.size > MAX_TEXT_BYTES) throw new Error('The text file exceeds 64 KiB. Use a shell command to read a smaller part.');
      const content = await sandbox.fs.read(resolved);
      if (Buffer.byteLength(content) > MAX_TEXT_BYTES) throw new Error('The text file exceeds 64 KiB.');
      return { sandboxId: sandbox.sandboxId, path: resolved, content };
    });
  }

  write(sessionID: string, signal: AbortSignal, path: string, content: string) {
    const resolved = remotePath(path);
    if (Buffer.byteLength(content) > MAX_TEXT_BYTES) throw new Error('The text file exceeds 64 KiB.');
    return this.run(sessionID, signal, async sandbox => {
      await sandbox.fs.write(resolved, content);
      return { sandboxId: sandbox.sandboxId, path: resolved, bytes: Buffer.byteLength(content) };
    });
  }

  close(sessionID: string) {
    return this.queue(sessionID, async () => ({ closed: await this.destroy(sessionID) }));
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    await Promise.allSettled([...this.queues.values()]);
    const results = await Promise.allSettled([...this.sandboxes.keys()].map(id => this.close(id)));
    const errors = results.filter(result => result.status === 'rejected').map(result => result.reason);
    if (errors.length) throw new AggregateError(errors, 'Some sandboxes could not be destroyed. Their lifetime limits still apply.');
  }
}
