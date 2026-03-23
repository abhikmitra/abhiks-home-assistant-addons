/**
 * Claude Terminal API Server
 *
 * A lightweight HTTP API server that accepts queries via POST /api/query,
 * spawns `claude -p` to process them, and returns the response.
 * Designed to run alongside ttyd in the Home Assistant add-on container.
 *
 * No external dependencies - uses only Node.js built-in modules.
 */

const http = require('http');
const { spawn } = require('child_process');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const API_PORT = 8099;
const API_HOST = '0.0.0.0';
const CLAUDE_TIMEOUT_MS = 120_000; // 120 seconds
const RATE_LIMIT_WINDOW_MS = 60_000; // 1 minute
const RATE_LIMIT_MAX = 10;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let busy = false;
const requestTimestamps = [];

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function log(level, message) {
    const ts = new Date().toISOString();
    console.log(`[API] ${ts} [${level.toUpperCase()}] ${message}`);
}

// ---------------------------------------------------------------------------
// Rate Limiting
// ---------------------------------------------------------------------------

/**
 * Returns true if the rate limit has been exceeded.
 * Prunes timestamps older than RATE_LIMIT_WINDOW_MS as a side-effect.
 */
function isRateLimited() {
    const now = Date.now();
    // Prune old timestamps
    while (requestTimestamps.length > 0 && requestTimestamps[0] <= now - RATE_LIMIT_WINDOW_MS) {
        requestTimestamps.shift();
    }
    return requestTimestamps.length >= RATE_LIMIT_MAX;
}

// ---------------------------------------------------------------------------
// System Prompt Builder
// ---------------------------------------------------------------------------

/**
 * Builds a dynamic system prompt based on request context.
 * @param {object} context - The context object from the request
 * @returns {string} The assembled system prompt
 */
function buildSystemPrompt(context) {
    if (!context) context = {};

    const now = new Date().toLocaleString('en-US', {
        timeZone: context.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone,
        dateStyle: 'full',
        timeStyle: 'long',
    });

    const source = context.source || 'conversation';
    const language = context.language || 'en';
    const parts = [];

    if (source === 'ai_task') {
        parts.push('You are responding via Home Assistant\'s AI Task interface, not an interactive terminal.');
        parts.push(`Current time: ${now}`);
        if (context.task_name) parts.push(`Task: ${context.task_name}`);
        parts.push(`Language: ${language}`);
        parts.push('');
        parts.push('Structure your output clearly as it will be consumed by automations.');
    } else {
        // conversation (default)
        parts.push('You are responding via Home Assistant\'s conversation interface, not an interactive terminal.');
        parts.push(`Current time: ${now}`);
        if (context.user_name) parts.push(`User: ${context.user_name}`);
        if (context.device_name) parts.push(`Triggered from device: ${context.device_name}`);
        if (context.satellite_name) parts.push(`Satellite: ${context.satellite_name}`);
        parts.push(`Language: ${language}`);
        parts.push('');
        parts.push('Be concise and action-oriented. When controlling devices, confirm what you did in one sentence.');
    }

    if (context.extra_system_prompt) {
        parts.push('');
        parts.push(context.extra_system_prompt);
    }

    return parts.join('\n');
}

// ---------------------------------------------------------------------------
// Argument Builder
// ---------------------------------------------------------------------------

/**
 * Builds the argument array for the `claude` CLI invocation.
 * @param {object} params - { query, conversation_id, context, json_schema }
 * @returns {string[]} Array of CLI arguments
 */
function buildClaudeArgs(params) {
    const { query, conversation_id, context, json_schema } = params;
    const args = [
        '-p',
        query,
        '--dangerously-skip-permissions',
        '--output-format',
        'json',
    ];

    if (conversation_id) {
        args.push('--resume', conversation_id);
    }

    if (json_schema) {
        args.push('--json-schema', typeof json_schema === 'string' ? json_schema : JSON.stringify(json_schema));
    }

    const source = (context && context.source) || 'conversation';
    if (source === 'ai_task' && !conversation_id) {
        args.push('--no-session-persistence');
    }

    const systemPrompt = buildSystemPrompt(context);
    args.push('--append-system-prompt', systemPrompt);

    return args;
}

// ---------------------------------------------------------------------------
// Response Extractor
// ---------------------------------------------------------------------------

/**
 * Extracts relevant fields from the claude JSON output.
 * @param {object} parsed - Parsed JSON from claude stdout
 * @param {boolean} hasSchema - Whether a json_schema was provided
 * @returns {object} { result, session_id, cost_usd, model_usage }
 */
function extractResponse(parsed, hasSchema) {
    if (!parsed || typeof parsed !== 'object') {
        return { result: '', session_id: null, cost_usd: 0, model_usage: {} };
    }

    let result;
    if (hasSchema) {
        result = parsed.structured_output !== undefined ? parsed.structured_output : (parsed.result || '');
    } else {
        result = parsed.result !== undefined ? parsed.result : '';
    }

    // If result is an object, stringify it
    if (typeof result === 'object' && result !== null) {
        result = JSON.stringify(result);
    }

    return {
        result: result,
        session_id: parsed.session_id || null,
        cost_usd: parsed.cost_usd || 0,
        model_usage: parsed.model_usage || {},
    };
}

// ---------------------------------------------------------------------------
// Request Handler Helpers
// ---------------------------------------------------------------------------

function sendJSON(res, statusCode, body) {
    const payload = JSON.stringify(body);
    res.writeHead(statusCode, {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
    });
    res.end(payload);
}

function readBody(req) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        req.on('data', (chunk) => chunks.push(chunk));
        req.on('end', () => resolve(Buffer.concat(chunks).toString()));
        req.on('error', reject);
    });
}

// ---------------------------------------------------------------------------
// Claude Process Runner
// ---------------------------------------------------------------------------

function runClaude(args) {
    return new Promise((resolve, reject) => {
        const startTime = Date.now();
        log('info', `Spawning claude with ${args.length} args`);

        const proc = spawn('claude', args, {
            timeout: CLAUDE_TIMEOUT_MS,
            stdio: ['ignore', 'pipe', 'pipe'],
            env: { ...process.env },
        });

        const stdoutChunks = [];
        const stderrChunks = [];

        proc.stdout.on('data', (d) => stdoutChunks.push(d));
        proc.stderr.on('data', (d) => stderrChunks.push(d));

        proc.on('error', (err) => {
            const duration = Date.now() - startTime;
            log('error', `Claude process error after ${duration}ms: ${err.message}`);
            reject(err);
        });

        proc.on('close', (code) => {
            const duration = Date.now() - startTime;
            const stdout = Buffer.concat(stdoutChunks).toString();
            const stderr = Buffer.concat(stderrChunks).toString();

            log('info', `Claude process exited with code ${code} after ${duration}ms`);

            if (code !== 0) {
                const errMsg = stderr || stdout || `Process exited with code ${code}`;
                log('error', `Claude process failed: ${errMsg}`);
                reject(new Error(errMsg));
                return;
            }

            resolve(stdout);
        });
    });
}

// ---------------------------------------------------------------------------
// Route Handlers
// ---------------------------------------------------------------------------

async function handleQuery(req, res) {
    // Check concurrency
    if (busy) {
        log('warn', 'Rejecting request: server is busy');
        sendJSON(res, 503, {
            error: true,
            message: 'Another request is currently being processed.',
            code: 503,
        });
        return;
    }

    // Check rate limit
    if (isRateLimited()) {
        log('warn', 'Rejecting request: rate limit exceeded');
        sendJSON(res, 429, {
            error: true,
            message: 'Rate limit exceeded. Max 10 requests per minute.',
            code: 429,
        });
        return;
    }

    // Record timestamp for rate limiting
    requestTimestamps.push(Date.now());

    // Parse body
    let body;
    try {
        const raw = await readBody(req);
        body = JSON.parse(raw);
    } catch (err) {
        log('error', `Invalid JSON body: ${err.message}`);
        sendJSON(res, 400, { error: true, message: 'Invalid JSON body.', code: 400 });
        return;
    }

    // Validate required fields
    if (!body.query || typeof body.query !== 'string') {
        log('error', 'Missing or invalid "query" field');
        sendJSON(res, 400, { error: true, message: 'Missing required field: query', code: 400 });
        return;
    }

    const context = body.context || {};
    const source = context.source || 'conversation';
    log('info', `Incoming request: source=${source}, query_length=${body.query.length}, has_conversation_id=${!!body.conversation_id}`);

    busy = true;
    try {
        const args = buildClaudeArgs({
            query: body.query,
            conversation_id: body.conversation_id,
            context: body.context,
            json_schema: body.json_schema,
        });

        const stdout = await runClaude(args);

        let parsed;
        try {
            parsed = JSON.parse(stdout);
        } catch (err) {
            log('error', `Failed to parse Claude output as JSON: ${err.message}`);
            log('error', `Raw output (first 500 chars): ${stdout.substring(0, 500)}`);
            sendJSON(res, 500, {
                error: true,
                message: 'Failed to parse Claude response as JSON.',
                code: 500,
            });
            return;
        }

        const hasSchema = !!body.json_schema;
        const extracted = extractResponse(parsed, hasSchema);

        log('info', `Response: session_id=${extracted.session_id}, result_length=${String(extracted.result).length}, cost=${extracted.cost_usd}`);

        sendJSON(res, 200, extracted);
    } catch (err) {
        log('error', `Request failed: ${err.message}`);
        sendJSON(res, 500, {
            error: true,
            message: err.message || 'Internal server error',
            code: 500,
        });
    } finally {
        busy = false;
    }
}

function handleHealth(_req, res) {
    sendJSON(res, 200, { status: 'ok', busy });
}

// ---------------------------------------------------------------------------
// Main Server
// ---------------------------------------------------------------------------

function requestHandler(req, res) {
    const { method, url } = req;

    if (method === 'POST' && url === '/api/query') {
        handleQuery(req, res).catch((err) => {
            log('error', `Unhandled error in handleQuery: ${err.message}`);
            if (!res.headersSent) {
                sendJSON(res, 500, { error: true, message: 'Internal server error', code: 500 });
            }
        });
        return;
    }

    if (method === 'GET' && url === '/api/health') {
        handleHealth(req, res);
        return;
    }

    sendJSON(res, 404, { error: true, message: 'Not found', code: 404 });
}

// ---------------------------------------------------------------------------
// Startup & Error Handling (only when run directly)
// ---------------------------------------------------------------------------

if (require.main === module) {
    const server = http.createServer(requestHandler);

    server.listen(API_PORT, API_HOST, () => {
        log('info', `API server listening on ${API_HOST}:${API_PORT}`);
    });

    process.on('uncaughtException', (err) => {
        log('error', `Uncaught exception: ${err.message}`);
        log('error', err.stack || '');
        log('info', 'Exiting in 5 seconds for restart...');
        setTimeout(() => process.exit(1), 5000);
    });

    process.on('unhandledRejection', (reason) => {
        log('error', `Unhandled rejection: ${reason}`);
    });
}

// ---------------------------------------------------------------------------
// Exports for testing
// ---------------------------------------------------------------------------

module.exports = {
    buildSystemPrompt,
    buildClaudeArgs,
    extractResponse,
    isRateLimited,
};
