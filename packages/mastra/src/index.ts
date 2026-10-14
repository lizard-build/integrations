import { randomUUID } from 'node:crypto';
import { posix } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { Sandbox, NotFoundError } from '@lizard-build/sdk';
import {
  SandboxAbortError,
  SandboxTimeoutError,
  SandboxUnsupportedFeatureError,
  validateSandboxFileMode,
} from '@mastra/core/workspace';
import type {
  WorkspaceSandbox, ProviderStatus, CommandResult, ExecuteCommandOptions,
  SandboxFileInput, WriteFilesOptions, SandboxInfo, SandboxStartResult,
} from '@mastra/core/workspace';
import { WORKER_SOURCE } from './worker.js';

export interface LizardSandboxOptions {
  projectId?: string;
  apiKey?: string;
  apiUrl?: string;
  size?: 'small' | 'medium' | 'large';
  lifetimeMs?: number;
  workingDirectory?: string;
  env?: Record<string, string>;
  id?: string;
}

interface WorkerResult {
  returncode: number;
  error?: string;
  message?: string;
  stdout_size: number;
  stderr_size: number;
  stdout_truncated?: boolean;
  stderr_truncated?: boolean;
  stdout_dropped_bytes?: number;
  stderr_dropped_bytes?: number;
}

/** One owned sandbox with explicit lifecycle and command cancellation. */
export class LizardSandbox implements WorkspaceSandbox {
  readonly id: string;
  readonly name = 'Lizard Sandboxes';
  readonly provider = 'lizard';
  readonly supportsCheckpoints = false;
  status: ProviderStatus = 'pending';
  error?: string;
  private native?: Sandbox;
  private options: LizardSandboxOptions & { projectId: string };
  private transition: Promise<unknown> = Promise.resolve();
  private root = '/tmp/mastra-lizard-' + randomUUID();
  private createdAt = new Date();

  constructor(options: LizardSandboxOptions = {}) {
    const projectId = options.projectId || process.env.LIZARD_PROJECT_ID;
    if (!projectId) throw new Error('Set LIZARD_PROJECT_ID or pass projectId.');
    if (options.lifetimeMs !== undefined && options.lifetimeMs <= 0) {
      throw new Error('lifetimeMs must be positive.');
    }
    this.options = { ...options, projectId };
    this.id = options.id ?? randomUUID();
  }

  get sandboxId(): string | undefined { return this.native?.sandboxId; }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const task = this.transition.then(operation, operation);
    this.transition = task;
    return task;
  }

  async start(): Promise<SandboxStartResult> {
    return this.serialize(async () => {
      if (this.status === 'destroyed') throw new Error('The sandbox is destroyed.');
      if (this.status === 'running') return { outcome: 'connected' };
      if (this.status === 'error' && this.native) {
        await this.native.kill();
        this.native = undefined;
      }
      this.status = 'starting';
      try {
        if (this.native) {
          await this.native.resume();
          this.status = 'running';
          return { outcome: 'connected' };
        }
        this.native = await Sandbox.create('interpreter', {
          projectId: this.options.projectId,
          apiKey: this.options.apiKey,
          apiUrl: this.options.apiUrl,
          size: this.options.size ?? 'small',
          timeoutMs: this.options.lifetimeMs ?? 1_800_000,
          metadata: { integration: 'mastra', adapterId: this.id },
        });
        await this.native.fs.write(this.root + '/worker.py', WORKER_SOURCE);
        const initialized = await this.native.process.exec(['/bin/mkdir', '-p', this.cwd]);
        if (initialized.exitCode !== 0) throw new Error(initialized.stderr);
        this.createdAt = new Date();
        this.status = 'running';
        this.error = undefined;
        return { outcome: 'created' };
      } catch (error) {
        this.status = 'error';
        this.error = String(error);
        if (this.native) {
          try { await this.native.kill(); this.native = undefined; }
          catch { /* Keep the handle so destroy() can retry cleanup. */ }
        }
        throw error;
      }
    });
  }

  private get cwd(): string { return this.options.workingDirectory ?? this.root + '/workspace'; }

  async stop(): Promise<void> {
    return this.serialize(async () => {
      if (!this.native || this.status === 'stopped' || this.status === 'destroyed') return;
      await this.native.pause();
      this.status = 'stopped';
    });
  }

  async destroy(): Promise<void> {
    return this.serialize(async () => {
      if (this.native) await this.native.kill();
      this.native = undefined;
      this.status = 'destroyed';
    });
  }

  async snapshot(): Promise<void> { /* Checkpoints are not part of this adapter. */ }

  getInfo(): SandboxInfo {
    return { id: this.id, name: this.name, provider: this.provider, status: this.status,
      createdAt: this.createdAt, metadata: { sandboxId: this.sandboxId } };
  }

  getInstructions(): string {
    return `Commands and files run in Lizard Sandboxes using the interpreter template. ` +
      `The working directory is ${this.cwd}. Files last until destroy() or the sandbox deadline.`;
  }

  async executeCommand(command: string, args: string[] = [], options: ExecuteCommandOptions = {}): Promise<CommandResult> {
    if (options.onStdout || options.onStderr) {
      throw new SandboxUnsupportedFeatureError('Live output callbacks are not supported.');
    }
    if (options.abortSignal?.aborted) throw new SandboxAbortError('Command aborted.');
    if (options.timeout !== undefined && (!Number.isFinite(options.timeout) || options.timeout <= 0)) {
      throw new Error('timeout must be a positive number of milliseconds.');
    }
    const limit = options.maxRetainedBytes ?? 10 * 1024 * 1024;
    if (limit < 0 || (!Number.isInteger(limit) && limit !== Infinity)) {
      throw new Error('maxRetainedBytes must be a nonnegative integer or Infinity.');
    }
    await this.start();
    const sandbox = this.native!;
    const job = this.root + '/job-' + randomUUID();
    const started = Date.now();
    const request = {
      cmd: args.length ? [command, ...args] : ['/bin/sh', '-c', command],
      input: '', cwd: posix.resolve(this.cwd, options.cwd ?? '.'),
      env: Object.fromEntries(Object.entries({ ...this.options.env, ...options.env }).filter(([,v]) => v !== undefined)),
      timeout: options.timeout === undefined ? null : options.timeout / 1000,
      output_limit: limit === Infinity ? null : limit,
    };
    await sandbox.fs.write(job + '/request.json', JSON.stringify(request));
    let pid: number | undefined;
    let completed = false;
    try {
      const launch = await sandbox.process.exec(['/usr/local/bin/python3', this.root + '/worker.py', 'start', job]);
      if (launch.exitCode !== 0) throw new Error('Command worker failed: ' + launch.stderr);
      pid = JSON.parse(launch.stdout).pid;
      let result: WorkerResult;
      while (true) {
        if (options.abortSignal?.aborted) throw new SandboxAbortError('Command aborted.');
        try { result = JSON.parse(await sandbox.fs.read(job + '/result.json')); completed = true; break; }
        catch (error) { if (!(error instanceof NotFoundError)) throw error; }
        if (Date.now() - started > (options.timeout ?? this.options.lifetimeMs ?? 1_800_000) + 30_000) {
          throw new SandboxTimeoutError(options.timeout ?? 1_800_000, 'command');
        }
        await delay(100);
      }
      if (result.error === 'timeout') throw new SandboxTimeoutError(options.timeout ?? 1_800_000, 'command');
      if (result.error) throw new Error(result.message ?? result.error);
      const stdout = (await this.download(sandbox, job + '/stdout', result.stdout_size)).toString('utf8');
      const stderr = (await this.download(sandbox, job + '/stderr', result.stderr_size)).toString('utf8');
      return { command, args, success: result.returncode === 0, exitCode: result.returncode,
        stdout, stderr, executionTimeMs: Date.now() - started,
        stdoutTruncated: result.stdout_truncated, stderrTruncated: result.stderr_truncated,
        stdoutDroppedBytes: result.stdout_dropped_bytes, stderrDroppedBytes: result.stderr_dropped_bytes };
    } catch (error) {
      if (pid !== undefined && !completed) {
        try {
          await sandbox.process.exec(['/usr/local/bin/python3', this.root + '/worker.py', 'stop', String(pid)]);
          for (let i = 0; i < 50; i++) {
            try { await sandbox.fs.read(job + '/result.json'); break; }
            catch (pollError) { if (!(pollError instanceof NotFoundError)) break; }
            await delay(100);
          }
        } catch { /* destroy() and the fixed lifetime bound any remaining process. */ }
      }
      throw error;
    } finally {
      try { await sandbox.process.exec(['/bin/rm', '-rf', job]); }
      catch { /* Sandbox cleanup removes remaining temporary files. */ }
    }
  }

  private async upload(sandbox: Sandbox, path: string, contents: string | Uint8Array): Promise<void> {
    const data = Buffer.from(contents);
    const chunkSize = 4 * 1024 * 1024;
    if (data.length <= chunkSize) { await sandbox.fs.write(path, data); return; }
    const temporary = this.root + '/upload-' + randomUUID();
    try {
      for (let offset = 0; offset < data.length; offset += chunkSize) {
        await sandbox.fs.write(temporary, data.subarray(offset, offset + chunkSize));
        const request = JSON.stringify({ source: temporary, target: path, offset });
        const result = await sandbox.process.exec(['/usr/local/bin/python3', this.root + '/worker.py', 'write_chunk', request]);
        if (result.exitCode !== 0) throw new Error(result.stderr);
      }
    } finally {
      await sandbox.process.exec(['/bin/rm', '-f', temporary]);
    }
  }

  private async download(sandbox: Sandbox, path: string, size: number): Promise<Buffer> {
    const chunkSize = 8 * 1024 * 1024;
    if (size <= chunkSize) return Buffer.from(await sandbox.fs.readBytes(path));
    const target = path + '.chunk';
    const chunks: Buffer[] = [];
    for (let offset = 0; offset < size; offset += chunkSize) {
      const request = JSON.stringify({ path, target, offset, length: chunkSize });
      const result = await sandbox.process.exec(['/usr/local/bin/python3', this.root + '/worker.py', 'chunk', request]);
      if (result.exitCode !== 0) throw new Error(result.stderr);
      chunks.push(Buffer.from(await sandbox.fs.readBytes(target)));
    }
    return Buffer.concat(chunks);
  }

  async writeFiles(files: SandboxFileInput[], options: WriteFilesOptions = {}): Promise<void> {
    for (const file of files) if (file.mode !== undefined) validateSandboxFileMode(file.mode);
    if (options.abortSignal?.aborted) throw new SandboxAbortError('File write aborted.');
    await this.start();
    for (const file of files) {
      if (options.abortSignal?.aborted) throw new SandboxAbortError('File write aborted.');
      const path = posix.resolve(this.cwd, file.path);
      await this.upload(this.native!, path, file.content);
      if (file.mode !== undefined) {
        const result = await this.native!.process.exec(['/bin/chmod', file.mode.toString(8), '--', path]);
        if (result.exitCode !== 0) throw new Error(result.stderr);
      }
    }
  }
}
