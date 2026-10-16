import { tool, type Plugin, type ToolContext } from '@opencode-ai/plugin';
import { descriptions, optionsFrom, SandboxRuntime } from './runtime.js';

async function allow(context: ToolContext, operation: string, detail: string) {
  context.abort.throwIfAborted();
  await context.ask({ permission: 'lizard_sandbox', patterns: [operation], always: [operation], metadata: { operation, detail } });
  context.abort.throwIfAborted();
}

const plugin: Plugin = async (_input, options) => {
  const runtime = new SandboxRuntime(optionsFrom(options));
  return {
    dispose: () => runtime.dispose(),
    event: async ({ event }) => {
      if (event.type !== 'session.deleted') return;
      // 1.19 bridges V2 events, while older releases use properties.info.id.
      const properties: unknown = event.properties;
      if (properties && typeof properties === 'object' && 'sessionID' in properties && typeof properties.sessionID === 'string') {
        await runtime.close(properties.sessionID);
        return;
      }
      await runtime.close(event.properties.info.id);
    },
    tool: {
      lizard_sandbox_create: tool({ description: descriptions.create, args: {}, execute: async (_, ctx) => {
        await allow(ctx, 'create', 'Create a sandbox for this session.');
        return JSON.stringify(await runtime.create(ctx.sessionID, ctx.abort));
      } }),
      lizard_sandbox_exec: tool({ description: descriptions.exec, args: {
        command: tool.schema.string().min(1), cwd: tool.schema.string().optional(), timeoutMs: tool.schema.number().int().min(1).max(120000).optional(),
      }, execute: async (args, ctx) => {
        await allow(ctx, 'exec', args.command);
        return JSON.stringify(await runtime.exec(ctx.sessionID, ctx.abort, args));
      } }),
      lizard_sandbox_read: tool({ description: descriptions.read, args: { path: tool.schema.string().min(1) }, execute: async (args, ctx) => {
        await allow(ctx, 'read', args.path);
        return JSON.stringify(await runtime.read(ctx.sessionID, ctx.abort, args.path));
      } }),
      lizard_sandbox_write: tool({ description: descriptions.write, args: { path: tool.schema.string().min(1), content: tool.schema.string() }, execute: async (args, ctx) => {
        await allow(ctx, 'write', args.path);
        return JSON.stringify(await runtime.write(ctx.sessionID, ctx.abort, args.path, args.content));
      } }),
      lizard_sandbox_close: tool({ description: descriptions.close, args: {}, execute: async (_, ctx) => {
        await allow(ctx, 'close', 'Destroy this session’s sandbox and its files.');
        return JSON.stringify(await runtime.close(ctx.sessionID));
      } }),
    },
  };
};

export default plugin;
