#!/usr/bin/env node
// A local OpenCLI boundary fixture. Every credential here is synthetic.
const vm = require('node:vm');
const fs = require('node:fs');

const args = process.argv.slice(2);
const command = args[2];
const tokenPrefix = 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL2NsZXJrLnN1bm8uY29tIiwiZXhwIjo0MTAyNDQ0ODAwfQ';
const fakeToken = tokenPrefix + '.signature';

if (command === 'open') {
  process.stdout.write(JSON.stringify({ url: 'https://suno.com/' }));
} else if (command === 'eval') {
  let evalCount = 0;
  if (['project-second-invalid', 'expired-first'].includes(process.env.FENGLING_TEST_SCENARIO)) {
    const state = process.env.FENGLING_TEST_STATE_FILE;
    evalCount = fs.existsSync(state) ? Number(fs.readFileSync(state, 'utf8')) : 0;
    fs.writeFileSync(state, String(++evalCount));
  }
  globalThis.window = globalThis;
  globalThis.location = {
    href: process.env.FENGLING_TEST_SCENARIO === 'sensitive-url'
      ? 'https://suno.com/discover?__clerk_handshake=synthetic-sensitive-value#private'
      : 'https://suno.com/discover',
  };
  globalThis.localStorage = { getItem(key) { return this[key] || ''; } };
  if (evalCount > 1) {
    globalThis.localStorage['studio-project-management-project-info-fixture'] =
      JSON.stringify({ projectId: 'fixture-project' });
  }

  if (process.env.FENGLING_TEST_SCENARIO === 'legacy') {
    globalThis.Clerk = { session: { getToken: async () => fakeToken } };
  } else if (['current', 'delayed', 'changed', 'sensitive-url', 'invalid-token', 'invalid-jwt', 'bad-three', 'project-second-invalid', 'expired-first', 'network-error', 'transient-503', 'transient-429', 'transient-network', 'html-response', 'wrong-json', 'forbidden', 'permanent-503', 'rate-limited-long', 'rate-limited-date', 'rate-limited-invalid'].includes(process.env.FENGLING_TEST_SCENARIO)) {
    const factories = new Map();
    const runtime = {
      async push(chunk) {
        if (chunk.length > 2) {
          factories.set(chunk[1], chunk[2]);
          return;
        }
        for (const id of chunk[1].runtimeModuleIds) {
          factories.get(id)({
            i(moduleId) {
              if (process.env.FENGLING_TEST_SCENARIO === 'changed' || moduleId !== 79076) {
                throw new Error('module unavailable');
              }
              return { t: async () => {
                if (process.env.FENGLING_TEST_SCENARIO === 'invalid-token') return 'not-a-credential';
                if (process.env.FENGLING_TEST_SCENARIO === 'bad-three') return 'foo.bar.baz';
                if (process.env.FENGLING_TEST_SCENARIO === 'invalid-jwt') return tokenPrefix + '.invalidsignature';
                if (process.env.FENGLING_TEST_SCENARIO === 'project-second-invalid' && evalCount > 1) return tokenPrefix + '.invalidsignature';
                if (process.env.FENGLING_TEST_SCENARIO === 'expired-first' && evalCount === 1) return tokenPrefix + '.expiredsignature';
                return fakeToken;
              } };
            },
          });
        }
      },
    };
    if (process.env.FENGLING_TEST_SCENARIO === 'delayed') {
      setTimeout(() => { globalThis.TURBOPACK = runtime; }, 150);
    } else {
      globalThis.TURBOPACK = runtime;
    }
  }

  Promise.resolve(vm.runInThisContext(args[3], { timeout: 1000 }))
    .then(value => process.stdout.write(String(value)))
    .catch(error => {
      process.stderr.write(String(error));
      process.exitCode = 1;
    });
} else {
  process.stderr.write('Unsupported fake OpenCLI command');
  process.exitCode = 1;
}
