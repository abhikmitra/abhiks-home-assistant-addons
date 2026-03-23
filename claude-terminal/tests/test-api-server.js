/**
 * Unit tests for the API server.
 *
 * Uses only Node.js built-in test runner (node:test) and assert (node:assert/strict).
 * Run with: node --test tests/test-api-server.js
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const {
    buildSystemPrompt,
    buildClaudeArgs,
    extractResponse,
    isRateLimited,
} = require('../scripts/api-server');

// -------------------------------------------------------------------------
// buildSystemPrompt
// -------------------------------------------------------------------------

describe('buildSystemPrompt', () => {
    it('includes all conversation fields when provided', () => {
        const prompt = buildSystemPrompt({
            source: 'conversation',
            user_name: 'Abhik',
            device_name: 'Kitchen Speaker',
            satellite_name: 'Kitchen Satellite',
            language: 'en',
        });

        assert.ok(prompt.includes('conversation interface'), 'should mention conversation interface');
        assert.ok(prompt.includes('User: Abhik'), 'should include user_name');
        assert.ok(prompt.includes('Triggered from device: Kitchen Speaker'), 'should include device_name');
        assert.ok(prompt.includes('Satellite: Kitchen Satellite'), 'should include satellite_name');
        assert.ok(prompt.includes('Language: en'), 'should include language');
        assert.ok(prompt.includes('Be concise and action-oriented'), 'should include conversation closing guidance');
    });

    it('builds AI Task prompt with task_name', () => {
        const prompt = buildSystemPrompt({
            source: 'ai_task',
            task_name: 'summarize_energy',
            language: 'en',
        });

        assert.ok(prompt.includes('AI Task interface'), 'should mention AI Task interface');
        assert.ok(prompt.includes('Task: summarize_energy'), 'should include task_name');
        assert.ok(prompt.includes('Structure your output clearly'), 'should include ai_task closing guidance');
        assert.ok(!prompt.includes('conversation interface'), 'should NOT mention conversation interface');
    });

    it('appends extra_system_prompt when provided', () => {
        const prompt = buildSystemPrompt({
            source: 'conversation',
            language: 'en',
            extra_system_prompt: 'Always reply in JSON.',
        });

        assert.ok(prompt.includes('Always reply in JSON.'), 'should include the extra system prompt');
    });

    it('handles empty context without crashing', () => {
        assert.doesNotThrow(() => buildSystemPrompt({}));
        assert.doesNotThrow(() => buildSystemPrompt(null));
        assert.doesNotThrow(() => buildSystemPrompt(undefined));
    });

    it('defaults to conversation source when source is not provided', () => {
        const prompt = buildSystemPrompt({});
        assert.ok(prompt.includes('conversation interface'), 'should default to conversation');
    });

    it('includes current time', () => {
        const prompt = buildSystemPrompt({ source: 'conversation', language: 'en' });
        assert.ok(prompt.includes('Current time:'), 'should include current time header');
        // The time string should contain a year (basic sanity)
        assert.match(prompt, /Current time:.*\d{4}/, 'time should contain a 4-digit year');
    });

    it('includes current time for ai_task source', () => {
        const prompt = buildSystemPrompt({ source: 'ai_task', language: 'en' });
        assert.ok(prompt.includes('Current time:'), 'should include current time for ai_task');
    });

    it('omits optional fields when not provided', () => {
        const prompt = buildSystemPrompt({ source: 'conversation', language: 'de' });
        assert.ok(!prompt.includes('User:'), 'should not include User line');
        assert.ok(!prompt.includes('Triggered from device:'), 'should not include device line');
        assert.ok(!prompt.includes('Satellite:'), 'should not include satellite line');
        assert.ok(prompt.includes('Language: de'), 'should include provided language');
    });
});

// -------------------------------------------------------------------------
// buildClaudeArgs
// -------------------------------------------------------------------------

describe('buildClaudeArgs', () => {
    it('always includes -p, query, --dangerously-skip-permissions, --output-format json', () => {
        const args = buildClaudeArgs({ query: 'hello', context: {} });

        assert.ok(args.includes('-p'), 'should include -p');
        assert.ok(args.includes('hello'), 'should include the query');
        assert.ok(args.includes('--dangerously-skip-permissions'), 'should include permissions flag');
        assert.ok(args.includes('--output-format'), 'should include --output-format');
        const fmtIdx = args.indexOf('--output-format');
        assert.equal(args[fmtIdx + 1], 'json', 'output format should be json');
    });

    it('adds --resume when conversation_id is provided', () => {
        const args = buildClaudeArgs({
            query: 'hello',
            conversation_id: 'sess-123',
            context: {},
        });

        assert.ok(args.includes('--resume'), 'should include --resume');
        const idx = args.indexOf('--resume');
        assert.equal(args[idx + 1], 'sess-123', 'resume value should be conversation_id');
    });

    it('does NOT add --resume when conversation_id is absent', () => {
        const args = buildClaudeArgs({ query: 'hello', context: {} });
        assert.ok(!args.includes('--resume'), 'should not include --resume');
    });

    it('adds --json-schema when json_schema is provided (object)', () => {
        const schema = { type: 'object', properties: { answer: { type: 'string' } } };
        const args = buildClaudeArgs({ query: 'hello', json_schema: schema, context: {} });

        assert.ok(args.includes('--json-schema'), 'should include --json-schema');
        const idx = args.indexOf('--json-schema');
        assert.equal(args[idx + 1], JSON.stringify(schema), 'should JSON-stringify the schema');
    });

    it('adds --json-schema when json_schema is a string', () => {
        const schema = '{"type":"object"}';
        const args = buildClaudeArgs({ query: 'hello', json_schema: schema, context: {} });

        assert.ok(args.includes('--json-schema'), 'should include --json-schema');
        const idx = args.indexOf('--json-schema');
        assert.equal(args[idx + 1], schema, 'should pass string as-is');
    });

    it('adds --no-session-persistence for ai_task without conversation_id', () => {
        const args = buildClaudeArgs({
            query: 'summarize',
            context: { source: 'ai_task' },
        });

        assert.ok(args.includes('--no-session-persistence'), 'should include --no-session-persistence');
    });

    it('does NOT add --no-session-persistence for ai_task WITH conversation_id', () => {
        const args = buildClaudeArgs({
            query: 'summarize',
            conversation_id: 'sess-abc',
            context: { source: 'ai_task' },
        });

        assert.ok(!args.includes('--no-session-persistence'), 'should NOT include --no-session-persistence');
    });

    it('does NOT add --no-session-persistence for conversation source', () => {
        const args = buildClaudeArgs({
            query: 'hello',
            context: { source: 'conversation' },
        });

        assert.ok(!args.includes('--no-session-persistence'), 'should NOT include --no-session-persistence');
    });

    it('adds --append-system-prompt with dynamic prompt content', () => {
        const args = buildClaudeArgs({
            query: 'hello',
            context: { source: 'conversation', user_name: 'Abhik', language: 'en' },
        });

        assert.ok(args.includes('--append-system-prompt'), 'should include --append-system-prompt');
        const idx = args.indexOf('--append-system-prompt');
        const prompt = args[idx + 1];
        assert.ok(prompt.includes('conversation interface'), 'prompt should mention conversation');
        assert.ok(prompt.includes('User: Abhik'), 'prompt should include user_name');
    });
});

// -------------------------------------------------------------------------
// extractResponse
// -------------------------------------------------------------------------

describe('extractResponse', () => {
    it('extracts result for normal calls', () => {
        const parsed = {
            result: 'Turned off the lights.',
            session_id: 'sess-456',
            cost_usd: 0.02,
            model_usage: { input_tokens: 100, output_tokens: 50 },
        };

        const out = extractResponse(parsed, false);
        assert.equal(out.result, 'Turned off the lights.');
        assert.equal(out.session_id, 'sess-456');
        assert.equal(out.cost_usd, 0.02);
        assert.deepEqual(out.model_usage, { input_tokens: 100, output_tokens: 50 });
    });

    it('extracts structured_output when hasSchema=true', () => {
        const parsed = {
            result: 'some text',
            structured_output: { answer: 42 },
            session_id: 'sess-789',
            cost_usd: 0.01,
            model_usage: {},
        };

        const out = extractResponse(parsed, true);
        // structured_output is an object, should be stringified
        assert.equal(out.result, JSON.stringify({ answer: 42 }));
        assert.equal(out.session_id, 'sess-789');
    });

    it('falls back to result when structured_output is missing and hasSchema=true', () => {
        const parsed = {
            result: 'fallback text',
            session_id: 'sess-000',
            cost_usd: 0,
            model_usage: {},
        };

        const out = extractResponse(parsed, true);
        assert.equal(out.result, 'fallback text');
    });

    it('handles missing fields gracefully', () => {
        const out = extractResponse({}, false);
        assert.equal(out.result, '');
        assert.equal(out.session_id, null);
        assert.equal(out.cost_usd, 0);
        assert.deepEqual(out.model_usage, {});
    });

    it('handles null input gracefully', () => {
        const out = extractResponse(null, false);
        assert.equal(out.result, '');
        assert.equal(out.session_id, null);
        assert.equal(out.cost_usd, 0);
        assert.deepEqual(out.model_usage, {});
    });

    it('handles undefined input gracefully', () => {
        const out = extractResponse(undefined, false);
        assert.equal(out.result, '');
        assert.equal(out.session_id, null);
    });

    it('stringifies object result for normal calls', () => {
        const parsed = { result: { key: 'value' }, session_id: 'x' };
        const out = extractResponse(parsed, false);
        assert.equal(out.result, JSON.stringify({ key: 'value' }));
    });

    it('returns string structured_output as-is when hasSchema=true', () => {
        const parsed = { structured_output: 'plain string', session_id: 'x' };
        const out = extractResponse(parsed, true);
        assert.equal(out.result, 'plain string');
    });
});

// -------------------------------------------------------------------------
// isRateLimited
// -------------------------------------------------------------------------

describe('isRateLimited', () => {
    // NOTE: isRateLimited mutates a shared module-level array.  We cannot
    // fully isolate tests without re-requiring the module, but we can verify
    // the behaviour by understanding it reads/writes the shared array.

    it('returns a boolean', () => {
        const result = isRateLimited();
        assert.equal(typeof result, 'boolean');
    });
});
