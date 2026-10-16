import { Plugin } from '@opencode/plugin';
import { Schema } from 'effect';
import { descriptions, optionsFrom, SandboxRuntime } from './runtime.js';

export default Plugin.define({
  id: 'lizard-sandboxes',
  setup: async ctx => {
    const runtime = new SandboxRuntime(optionsFrom(ctx.options));
    await ctx.tool.transform(tools => {
      tools.add({ name: 'lizard_sandbox_create', options: { codemode: false }, description: descriptions.create,
        input: Schema.Struct({}), execute: async (_, context) => {
          context.signal.throwIfAborted();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent, action: 'lizard_sandbox', resources: ['create'], metadata: { operation: 'create' }, source: { type: 'tool', messageID: context.messageID, id: context.id } });
          return { content: JSON.stringify(await runtime.create(context.sessionID, context.signal)) };
        } });
      tools.add({ name: 'lizard_sandbox_exec', options: { codemode: false }, description: descriptions.exec,
        input: Schema.Struct({ command: Schema.String, cwd: Schema.optionalKey(Schema.String), timeoutMs: Schema.optionalKey(Schema.Number) }), execute: async (args, context) => {
          context.signal.throwIfAborted();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent, action: 'lizard_sandbox', resources: ['exec'], metadata: { command: args.command, cwd: args.cwd }, source: { type: 'tool', messageID: context.messageID, id: context.id } });
          return { content: JSON.stringify(await runtime.exec(context.sessionID, context.signal, args)) };
        } });
      tools.add({ name: 'lizard_sandbox_read', options: { codemode: false }, description: descriptions.read,
        input: Schema.Struct({ path: Schema.String }), execute: async (args, context) => {
          context.signal.throwIfAborted();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent, action: 'lizard_sandbox', resources: ['read'], metadata: { path: args.path }, source: { type: 'tool', messageID: context.messageID, id: context.id } });
          return { content: JSON.stringify(await runtime.read(context.sessionID, context.signal, args.path)) };
        } });
      tools.add({ name: 'lizard_sandbox_write', options: { codemode: false }, description: descriptions.write,
        input: Schema.Struct({ path: Schema.String, content: Schema.String }), execute: async (args, context) => {
          context.signal.throwIfAborted();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent, action: 'lizard_sandbox', resources: ['write'], metadata: { path: args.path }, source: { type: 'tool', messageID: context.messageID, id: context.id } });
          return { content: JSON.stringify(await runtime.write(context.sessionID, context.signal, args.path, args.content)) };
        } });
      tools.add({ name: 'lizard_sandbox_close', options: { codemode: false }, description: descriptions.close,
        input: Schema.Struct({}), execute: async (_, context) => {
          context.signal.throwIfAborted();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent, action: 'lizard_sandbox', resources: ['close'], metadata: { operation: 'close' }, source: { type: 'tool', messageID: context.messageID, id: context.id } });
          context.signal.throwIfAborted();
          return { content: JSON.stringify(await runtime.close(context.sessionID)) };
        } });
    });
    const controller = new AbortController();
    const events = (async () => {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        if (event.type === 'session.deleted') await runtime.close(event.data.sessionID);
      }
    })();
    void events.catch(error => {
      if (!controller.signal.aborted) console.warn('Lizard Sandboxes event cleanup stopped; sandbox lifetime limits still apply.', error instanceof Error ? error.name : 'Error');
    });
    return async () => {
      controller.abort();
      await runtime.dispose();
    };
  },
});
