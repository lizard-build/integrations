import { afterEach, describe, expect, it, vi } from 'vitest';
import { MAX_TEXT_BYTES, optionsFrom, SandboxRuntime, type RemoteSandbox } from '../src/runtime.js';

function remote(id = 'sandbox-1') {
  const files = new Map<string, string>();
  return {
    sandboxId: id,
    kill: vi.fn(async () => true),
    process: { exec: vi.fn(async () => ({ stdout: 'hello\n', stderr: '', exitCode: 0 })) },
    fs: {
      read: vi.fn(async (path: string) => files.get(path) ?? ''),
      write: vi.fn(async (path: string, value: string) => { files.set(path, value); }),
      stat: vi.fn(async (path: string) => ({ size: Buffer.byteLength(files.get(path) ?? '') })),
    },
  };
}
const signal = () => new AbortController().signal;
afterEach(() => vi.useRealTimers());

describe('session ownership', () => {
  it('serializes a session and gives other sessions their own sandbox', async () => {
    const sandboxes = [remote('one'), remote('two')];
    const create = vi.fn(async () => sandboxes[create.mock.calls.length - 1]!);
    const runtime = new SandboxRuntime({ create });
    const [a, b, c] = await Promise.all([
      runtime.create('a', signal()), runtime.exec('a', signal(), { command: 'pwd' }), runtime.create('b', signal()),
    ]);
    expect(a.sandboxId).toBe(b.sandboxId);
    expect(c.sandboxId).not.toBe(a.sandboxId);
    expect(create).toHaveBeenCalledTimes(2);
    await runtime.close('a');
    expect(sandboxes[0]!.kill).toHaveBeenCalledTimes(1);
    expect(sandboxes[1]!.kill).not.toHaveBeenCalled();
    await runtime.dispose();
    expect(sandboxes[1]!.kill).toHaveBeenCalledTimes(1);
    await expect(runtime.create('c', signal())).rejects.toThrow('stopped');
  });

  it('retains failed cleanup for retry', async () => {
    const sandbox = remote();
    const runtime = new SandboxRuntime({ create: async () => sandbox });
    await runtime.create('a', signal());
    sandbox.kill.mockRejectedValueOnce(new Error('network unavailable'));
    await expect(runtime.close('a')).rejects.toThrow('network');
    expect(await runtime.close('a')).toEqual({ closed: true });
    expect(await runtime.close('a')).toEqual({ closed: false });
  });

  it('does not replay a command after the sandbox expires', async () => {
    vi.useFakeTimers();
    const sandbox = remote();
    const runtime = new SandboxRuntime({ lifetimeMs: 60000, create: async () => sandbox });
    await runtime.create('a', signal());
    await vi.advanceTimersByTimeAsync(60001);
    await expect(runtime.exec('a', signal(), { command: 'touch marker' })).rejects.toThrow('expired');
    expect(sandbox.process.exec).not.toHaveBeenCalled();
    expect(sandbox.kill).toHaveBeenCalledTimes(1);
  });
});

describe('cancellation', () => {
  it('does not create a sandbox for an already cancelled call', async () => {
    const create = vi.fn(async () => remote());
    const runtime = new SandboxRuntime({ create });
    const controller = new AbortController();
    controller.abort();
    await expect(runtime.create('a', controller.signal)).rejects.toThrow();
    expect(create).not.toHaveBeenCalled();
  });

  it('cleans up a sandbox that finishes creating after cancellation', async () => {
    const sandbox = remote();
    let finish!: (value: RemoteSandbox) => void;
    const create = vi.fn(() => new Promise<RemoteSandbox>(resolve => { finish = resolve; }));
    const runtime = new SandboxRuntime({ create });
    const controller = new AbortController();
    const pending = runtime.create('a', controller.signal);
    await vi.waitFor(() => expect(create).toHaveBeenCalled());
    controller.abort();
    finish(sandbox);
    await expect(pending).rejects.toThrow();
    expect(sandbox.kill).toHaveBeenCalledTimes(1);
  });

  it('destroys a sandbox when a running command is cancelled', async () => {
    const sandbox = remote();
    sandbox.process.exec.mockImplementation(() => new Promise(() => {}));
    const runtime = new SandboxRuntime({ create: async () => sandbox });
    const controller = new AbortController();
    const pending = runtime.exec('a', controller.signal, { command: 'sleep 60' });
    await vi.waitFor(() => expect(sandbox.process.exec).toHaveBeenCalled());
    controller.abort(new Error('cancelled'));
    await expect(pending).rejects.toThrow('cancelled');
    expect(sandbox.kill).toHaveBeenCalledTimes(1);
  });

  it('destroys a sandbox on timeout, including a stalled SDK request', async () => {
    vi.useFakeTimers();
    const sandbox = remote();
    sandbox.process.exec.mockImplementation(() => new Promise(() => {}));
    const runtime = new SandboxRuntime({ create: async () => sandbox });
    const pending = expect(runtime.exec('a', signal(), { command: 'sleep 60', timeoutMs: 10 })).rejects.toThrow('timed out');
    await vi.advanceTimersByTimeAsync(11);
    await pending;
    expect(sandbox.kill).toHaveBeenCalledTimes(1);
  });
});

describe('files and output', () => {
  it('resolves remote paths, preserves UTF-8, and passes command options', async () => {
    const sandbox = remote();
    const runtime = new SandboxRuntime({ create: async () => sandbox });
    await runtime.write('a', signal(), 'a \'тест.txt', 'Привет 🦎');
    expect(await runtime.read('a', signal(), 'a \'тест.txt')).toMatchObject({ path: "/workspace/a 'тест.txt", content: 'Привет 🦎' });
    await runtime.exec('a', signal(), { command: 'printf hello', cwd: 'sub', timeoutMs: 1234 });
    expect(sandbox.process.exec).toHaveBeenCalledWith('printf hello', { workdir: '/workspace/sub', timeoutMs: 1234 });
    await runtime.dispose();
  });

  it('bounds text by UTF-8 bytes and rejects invalid options before creation', async () => {
    const create = vi.fn(async () => remote());
    const runtime = new SandboxRuntime({ create });
    expect(() => runtime.write('a', signal(), 'file', '🦎'.repeat(MAX_TEXT_BYTES))).toThrow('64 KiB');
    expect(() => runtime.exec('a', signal(), { command: 'ls', timeoutMs: -1 })).toThrow('timeoutMs');
    expect(() => optionsFrom({ lifetimeMs: 0 })).toThrow('lifetimeMs');
    expect(() => runtime.read('a', signal(), '\0')).toThrow('NUL');
    expect(create).not.toHaveBeenCalled();
  });

  it('rejects large reads and clips each output stream without splitting UTF-8', async () => {
    const sandbox = remote();
    const runtime = new SandboxRuntime({ create: async () => sandbox });
    sandbox.fs.stat.mockResolvedValue({ size: MAX_TEXT_BYTES + 1 });
    await expect(runtime.read('a', signal(), 'huge')).rejects.toThrow('64 KiB');
    expect(sandbox.fs.read).not.toHaveBeenCalled();
    sandbox.process.exec.mockResolvedValue({ stdout: '€'.repeat(MAX_TEXT_BYTES), stderr: 'e'.repeat(MAX_TEXT_BYTES + 1), exitCode: 7 });
    const result = await runtime.exec('a', signal(), { command: 'generate' });
    expect(result).toMatchObject({ truncated: true, exitCode: 7 });
    expect(Buffer.byteLength(result.stdout)).toBeLessThanOrEqual(MAX_TEXT_BYTES);
    expect(result.stdout).not.toContain('�');
    expect(Buffer.byteLength(result.stderr)).toBe(MAX_TEXT_BYTES);
    await runtime.dispose();
  });
});
