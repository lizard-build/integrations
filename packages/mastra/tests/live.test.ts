import { expect, it } from 'vitest';
import { Workspace, SandboxAbortError, SandboxTimeoutError } from '@mastra/core/workspace';
import { LizardSandbox } from '../src/index.js';

it.skipIf(process.env.LIZARD_LIVE_TESTS !== '1')('runs Workspace commands in the cloud', async () => {
  const sandbox = new LizardSandbox({ lifetimeMs: 300000 });
  const workspace = new Workspace({ sandbox });
  try {
    await workspace.init();
    await sandbox.writeFiles([{ path: 'a.bin', content: Buffer.from([0, 255, 10]), mode: 0o600 }]);
    const files = await sandbox.executeCommand('python3', ['-c',
      "import os; print(open('a.bin','rb').read().hex()); print(oct(os.stat('a.bin').st_mode & 0o777))"]);
    expect(files.stdout).toBe('00ff0a\n0o600\n');
    const output = await sandbox.executeCommand('python3', ['-c', "print('x'*700000)"]);
    expect(output.stdout.length).toBe(700001);
    await expect(sandbox.executeCommand('sleep 10', [], { timeout: 1000 })).rejects.toBeInstanceOf(SandboxTimeoutError);
    const controller = new AbortController();
    const pending = sandbox.executeCommand('sleep 5; touch cancelled', [], { abortSignal: controller.signal });
    const timer = setTimeout(() => controller.abort(), 1500);
    try { await expect(pending).rejects.toBeInstanceOf(SandboxAbortError); }
    finally { clearTimeout(timer); }
    await new Promise(resolve => setTimeout(resolve, 5000));
    expect((await sandbox.executeCommand('test ! -e cancelled')).success).toBe(true);
    await sandbox.stop();
    await sandbox.start();
    expect((await sandbox.executeCommand('cat a.bin | wc -c')).stdout.trim()).toBe('3');
  } finally {
    await workspace.destroy();
    await sandbox.destroy();
  }
}, 180000);
