import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { promisify } from 'node:util';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Workspace, SandboxAbortError, SandboxTimeoutError } from '@mastra/core/workspace';

const mocks = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock('@lizard-build/sdk', async (original) => ({
  ...await original<typeof import('@lizard-build/sdk')>(),
  Sandbox: { create: mocks.create },
}));
import { NotFoundError } from '@lizard-build/sdk';
import { LizardSandbox } from '../src/index.js';

const execute = promisify(execFile);
let provider: LizardSandbox;
let native: any;
let directory: string;

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'mastra-lizard-test-'));
  native = {
    sandboxId: 'unit', kill: vi.fn(), pause: vi.fn(), resume: vi.fn(),
    fs: {
      write: vi.fn(async (path: string, contents: string | Uint8Array) => {
        await mkdir(dirname(path), { recursive: true });
        await writeFile(path, contents);
      }),
      read: async (path: string) => {
        try { return await readFile(path, 'utf8'); }
        catch (error: any) { if (error.code === 'ENOENT') throw new NotFoundError(path); throw error; }
      },
      readBytes: async (path: string) => readFile(path),
    },
    process: { exec: vi.fn(async (cmd: string[]) => {
      try {
        if (cmd[0] === '/bin/chmod') cmd = cmd.filter(arg => arg !== '--');
        const result = await execute(cmd[0] === '/usr/local/bin/python3' ? 'python3' : cmd[0], cmd.slice(1));
        return { ...result, exitCode: 0 };
      } catch (error: any) {
        return { stdout: error.stdout, stderr: error.stderr, exitCode: error.code };
      }
    }) },
  };
  mocks.create.mockReset().mockResolvedValue(native);
  provider = new LizardSandbox({ projectId: 'test' });
  (provider as any).root = directory;
});
afterEach(async () => { await provider.destroy(); });

describe('WorkspaceSandbox', () => {
  it('works through Workspace and preserves argv, cwd, env and binary files', async () => {
    const workspace = new Workspace({ sandbox: provider });
    await workspace.init();
    await provider.writeFiles([{ path: 'a.bin', content: Buffer.from([0, 255, 10]) }]);
    const result = await workspace.sandbox.executeCommand('python3', ['-c',
      "import os,sys; print(os.getenv('VALUE')); print(sys.argv[1]); print(open('a.bin','rb').read().hex())",
      "literal ' $()"], { env: { VALUE: '42' } });
    expect(result.success).toBe(true);
    expect(result.stdout).toBe("42\nliteral ' $()\n00ff0a\n");
    await workspace.destroy();
    expect(native.kill).toHaveBeenCalledOnce();
  });

  it('keeps exit codes and stderr', async () => {
    const result = await provider.executeCommand('printf bad >&2; exit 7');
    expect(result).toMatchObject({ success: false, exitCode: 7, stderr: 'bad' });
  });

  it('serializes concurrent starts and resumes after stop', async () => {
    await Promise.all([provider.start(), provider.start()]);
    expect(mocks.create).toHaveBeenCalledOnce();
    await provider.stop();
    expect(provider.status).toBe('stopped');
    await provider.start();
    expect(native.resume).toHaveBeenCalledOnce();
    expect(provider.status).toBe('running');
  });

  it('retains output tails and reports dropped bytes', async () => {
    const result = await provider.executeCommand('python3', ['-c',
      "import sys; sys.stdout.write('x'*700000+'END'); sys.stderr.write('y'*700000+'ERR')"],
      { maxRetainedBytes: 600000 });
    expect(result.stdout).toHaveLength(600000);
    expect(result.stdout.endsWith('END')).toBe(true);
    expect(result.stderr.endsWith('ERR')).toBe(true);
    expect(result.stdoutDroppedBytes).toBe(100003);
    expect(result.stdoutTruncated).toBe(true);
  });

  it('times out and stops the command', async () => {
    await expect(provider.executeCommand('sleep 3; touch late', [], { timeout: 100 }))
      .rejects.toBeInstanceOf(SandboxTimeoutError);
    expect((await provider.executeCommand('echo alive')).stdout).toBe('alive\n');
  });

  it('kills the command on abort', async () => {
    const controller = new AbortController();
    const result = provider.executeCommand('sleep 1; touch aborted', [], { abortSignal: controller.signal });
    setTimeout(() => controller.abort(), 150);
    await expect(result).rejects.toBeInstanceOf(SandboxAbortError);
    await new Promise(resolve => setTimeout(resolve, 1100));
    expect((await provider.executeCommand('test ! -e aborted')).success).toBe(true);
  });

  it('rejects streaming and bad modes before creating a sandbox', async () => {
    await expect(provider.executeCommand('echo x', [], { onStdout: () => {} })).rejects.toThrow();
    await expect(provider.writeFiles([{ path: 'x', content: 'x', mode: -1 }])).rejects.toThrow();
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('applies file mode', async () => {
    await provider.writeFiles([{ path: 'private', content: 'data', mode: 0o600 }]);
    expect((await stat(join(directory, 'workspace/private'))).mode & 0o777).toBe(0o600);
  });

  it('retains a failed initialization for cleanup and rebuilds on retry', async () => {
    native.fs.write.mockRejectedValueOnce(new Error('write failed'));
    native.kill.mockRejectedValueOnce(new Error('kill failed'));
    await expect(provider.start()).rejects.toThrow('write failed');
    expect(provider.sandboxId).toBe('unit');
    await provider.start();
    expect(native.kill).toHaveBeenCalledTimes(2);
    expect(mocks.create).toHaveBeenCalledTimes(2);
    expect(native.resume).not.toHaveBeenCalled();
  });

  it('keeps failed destruction retryable', async () => {
    await provider.start();
    native.kill.mockRejectedValueOnce(new Error('network'));
    await expect(provider.destroy()).rejects.toThrow('network');
    expect(provider.sandboxId).toBe('unit');
    await provider.destroy();
    expect(provider.status).toBe('destroyed');
    await expect(provider.start()).rejects.toThrow('destroyed');
  });
});

it('downloads output above the SDK cap in chunks', async () => {
  native.fs.readBytes = async (path: string) => {
    const data = await readFile(path);
    expect(data.length).toBeLessThanOrEqual(8 * 1024 * 1024);
    return data;
  };
  const result = await provider.executeCommand('python3', ['-c', "print('x' * (16 * 1024 * 1024))"],
    { maxRetainedBytes: Infinity });
  expect(result.stdout.length).toBe(16 * 1024 * 1024 + 1);
  expect(result.stdoutTruncated).toBe(false);
});

it('uploads large files in chunks and truncates existing content', async () => {
  const original = native.fs.write;
  native.fs.write = async (path: string, contents: string | Uint8Array) => {
    expect(Buffer.byteLength(contents)).toBeLessThanOrEqual(4 * 1024 * 1024);
    return original(path, contents);
  };
  const content = Buffer.alloc(9 * 1024 * 1024, 195);
  await provider.writeFiles([{ path: 'large.bin', content }]);
  expect((await readFile(join(directory, 'workspace/large.bin'))).equals(content)).toBe(true);
  await provider.writeFiles([{ path: 'large.bin', content: content.subarray(0, 5 * 1024 * 1024) }]);
  expect((await stat(join(directory, 'workspace/large.bin'))).size).toBe(5 * 1024 * 1024);
});
