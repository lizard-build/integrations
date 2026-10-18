import { defineSandbox } from '@tanstack/ai-sandbox'
import { lizardSandbox } from '../src/index.js'

const sandbox = defineSandbox({
  id: 'code-workspace',
  provider: lizardSandbox({ lifetimeMs: 600_000 }),
  lifecycle: { reuse: 'thread', snapshot: 'none' },
})

const context = { threadId: 'example-thread', runId: 'example-run' }
try {
  const handle = await sandbox.ensure(context)
  await handle.fs.write('example.py', 'print(6 * 7)')
  const result = await handle.process.exec('python3 example.py')
  console.log(result.stdout)
} finally {
  await sandbox.destroy(context)
}
