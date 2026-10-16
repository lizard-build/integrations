import { SandboxRuntime } from '@lizard-build/opencode/runtime';

const runtime = new SandboxRuntime({ lifetimeMs: 300000 });
const signal = new AbortController().signal;
try {
  await runtime.write('example', signal, 'hello.py', 'print("Hello from a sandbox")\n');
  console.log(await runtime.exec('example', signal, { command: 'python3 hello.py' }));
} finally {
  await runtime.dispose();
}
