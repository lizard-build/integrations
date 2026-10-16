import { afterEach, expect, it, vi } from 'vitest';
import { Sandbox } from '@lizard-build/sdk';
import type { PluginInput, ToolContext } from '@opencode-ai/plugin';
import v1 from '../src/v1.js';
import v2 from '../src/index.js';
import { Schema } from 'effect';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); });
function setupSDK() {
  vi.stubEnv('LIZARD_API_KEY', 'test-key');
  vi.stubEnv('LIZARD_PROJECT_ID', 'test-project');
  const sandbox = { sandboxId: 'owned', kill: vi.fn(async () => true), process: { exec: vi.fn(async () => ({ stdout: 'ok', stderr: '', exitCode: 0 })) } };
  const create = vi.spyOn(Sandbox, 'create').mockResolvedValue(sandbox as unknown as Sandbox);
  return { sandbox, create };
}

it('v1 checks permissions before creating resources and cleans up on dispose', async () => {
  const { sandbox, create } = setupSDK();
  const hooks = await v1({} as PluginInput);
  const ask = vi.fn(async () => {});
  const context = { sessionID: 'session-a', abort: new AbortController().signal, ask } as unknown as ToolContext;
  ask.mockRejectedValueOnce(new Error('denied'));
  await expect(hooks.tool!.lizard_sandbox_exec!.execute({ command: 'ls' }, context)).rejects.toThrow('denied');
  expect(create).not.toHaveBeenCalled();
  expect(JSON.parse(await hooks.tool!.lizard_sandbox_exec!.execute({ command: 'ls' }, context) as string)).toMatchObject({ stdout: 'ok' });
  expect(ask).toHaveBeenLastCalledWith(expect.objectContaining({ permission: 'lizard_sandbox', patterns: ['exec'] }));
  expect(create).toHaveBeenCalledWith('interpreter', { projectId: 'test-project', apiKey: 'test-key', apiUrl: undefined, size: 'small', timeoutMs: 900000, metadata: { integration: 'opencode', sessionId: 'session-a' } });
  await hooks.dispose!();
  expect(sandbox.kill).toHaveBeenCalledTimes(1);
});

it('v2 registers actual tool schemas, checks permissions, and disposes resources', async () => {
  const { sandbox, create } = setupSDK();
  type TestTool = { name: string; input: Schema.Codec<Record<string, unknown>>; execute: (args: Record<string, unknown>, ctx: Record<string, unknown>) => Promise<{ content: string }> };
  const tools = new Map<string, TestTool>();
  const assert = vi.fn(async () => {});
  const ctx = {
    options: {},
    permission: { assert },
    tool: { transform: async (fn: (editor: { add: (tool: TestTool) => void }) => void) => fn({ add: tool => { tools.set(tool.name, tool); } }) },
    event: { subscribe: async function* () {} },
  };
  const cleanup = await v2.setup(ctx as unknown as Parameters<typeof v2.setup>[0]);
  expect([...tools.keys()]).toHaveLength(5);
  expect(() => Schema.decodeUnknownSync(tools.get('lizard_sandbox_exec')!.input)({ command: 123 })).toThrow();
  expect(Schema.decodeUnknownSync(tools.get('lizard_sandbox_exec')!.input)({ command: 'ls' })).toEqual({ command: 'ls' });
  const context = { sessionID: 'session-a', agent: 'agent', messageID: 'message', id: 'call', signal: new AbortController().signal };
  assert.mockRejectedValueOnce(new Error('denied'));
  await expect(tools.get('lizard_sandbox_create')!.execute({}, context)).rejects.toThrow('denied');
  expect(create).not.toHaveBeenCalled();
  const result = await tools.get('lizard_sandbox_exec')!.execute({ command: 'ls' }, context);
  expect(JSON.parse(result.content)).toMatchObject({ stdout: 'ok' });
  expect(assert).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'lizard_sandbox', resources: ['exec'], source: { type: 'tool', messageID: 'message', id: 'call' } }));
  if (typeof cleanup !== 'function') throw new Error('Expected plugin cleanup');
  await cleanup();
  expect(sandbox.kill).toHaveBeenCalledTimes(1);
});

it.each(['legacy', 'bridge'])('v1 cleans up a deleted session from %s events', async format => {
  const { sandbox } = setupSDK();
  const hooks = await v1({} as PluginInput);
  const ctx = { sessionID: 'session-a', abort: new AbortController().signal, ask: async () => {} } as unknown as ToolContext;
  await hooks.tool!.lizard_sandbox_create!.execute({}, ctx);
  const event = { type: 'session.deleted', properties: format === 'legacy' ? { info: { id: 'session-a' } } : { sessionID: 'session-a' } };
  await hooks.event!({ event } as Parameters<NonNullable<typeof hooks.event>>[0]);
  expect(sandbox.kill).toHaveBeenCalledTimes(1);
  await hooks.dispose!();
  expect(sandbox.kill).toHaveBeenCalledTimes(1);
});
