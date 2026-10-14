import { Workspace } from '@mastra/core/workspace';
import { LizardSandbox } from '@lizard-build/mastra';

const sandbox = new LizardSandbox();
const workspace = new Workspace({ sandbox });
try {
  await workspace.init();
  await sandbox.writeFiles([{ path: 'answer.py', content: 'print(6 * 7)\n' }]);
  const result = await sandbox.executeCommand('python3', ['answer.py'], { timeout: 30_000 });
  console.log(result.stdout);
} finally {
  await workspace.destroy();
  await sandbox.destroy();
}
