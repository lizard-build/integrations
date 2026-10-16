import { expect, it } from 'vitest';
import { SandboxRuntime } from '../src/runtime.js';

it.skipIf(process.env.LIZARD_LIVE_TESTS !== '1')('runs shell and file tools in separate cloud sandboxes and cancels a command', async () => {
  const runtime = new SandboxRuntime({ lifetimeMs: 300000 });
  const signal = new AbortController().signal;
  try {
    const a = await runtime.create('opencode-live-a', signal);
    const b = await runtime.create('opencode-live-b', signal);
    expect(a.sandboxId).not.toBe(b.sandboxId);
    await runtime.write('opencode-live-a', signal, 'hello.txt', 'Привет 🦎\n');
    expect((await runtime.read('opencode-live-a', signal, 'hello.txt')).content).toBe('Привет 🦎\n');
    expect((await runtime.exec('opencode-live-a', signal, { command: 'cat hello.txt' })).stdout).toBe('Привет 🦎\n');
    expect((await runtime.exec('opencode-live-b', signal, { command: 'test ! -e hello.txt' })).exitCode).toBe(0);
    const controller = new AbortController();
    const pending = runtime.exec('opencode-live-a', controller.signal, { command: 'sleep 60' });
    const timer = setTimeout(() => controller.abort(new Error('cancelled by test')), 2000);
    try { await expect(pending).rejects.toThrow('cancelled by test'); }
    finally { clearTimeout(timer); }
    expect(await runtime.close('opencode-live-a')).toEqual({ closed: false });
  } finally { await runtime.dispose(); }
}, 180000);
