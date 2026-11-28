// 测试用：照正式 DSH 宿主（dsh-tools、dsh-system-prompt）的规则检查工具定义、工具回传值、
// 会话作用域里的 tools.restrict／presentAs 与 systemPrompt.section／context。
// 宿主在这些地方丢错时，整个外挂不会启用（2026-11-28 部署时 draw_request 少了 output 就是这样），
// 假宿主不检查的话本机测试全过也抓不到，所以测试与重现脚本都改用这里的版本。
// 规则是照宿主的错误讯息与行为自己写的，不含宿主原始码。

const CONSTRAINT_KEYWORDS = new Set([
	'type',
	'oneOf',
	'properties',
	'required',
	'additionalProperties',
	'items',
	'enum',
	'const',
]);
const ANNOTATION_KEYWORDS = new Set([
	'description',
	'title',
	'default',
	'examples',
]);
const SCHEMA_TYPES = [
	'object',
	'array',
	'string',
	'number',
	'integer',
	'boolean',
	'null',
];
const SCALAR_TYPES = ['string', 'number', 'integer', 'boolean', 'null'];
const KEYWORD_TYPES = {
	properties: ['object'],
	required: ['object'],
	additionalProperties: ['object'],
	items: ['array'],
	enum: SCALAR_TYPES,
	const: SCALAR_TYPES,
};
const ONE_OF_SIBLINGS = [
	'properties',
	'required',
	'additionalProperties',
	'items',
	'enum',
	'const',
];
const RUN_CODE = 'run_code';

const isRecord = (value) =>
	typeof value === 'object' &&
	value !== null &&
	!Array.isArray(value) &&
	[Object.prototype, null].includes(Object.getPrototypeOf(value));
const isNumber = (value) =>
	typeof value === 'number' && Number.isFinite(value) && !Object.is(value, -0);

function scalarMatches(type, value) {
	if (type === 'string') return typeof value === 'string';
	if (type === 'number') return isNumber(value);
	if (type === 'integer') return isNumber(value) && Number.isInteger(value);
	if (type === 'boolean') return typeof value === 'boolean';
	return value === null;
}

/**
 * 输出 schema 只能用宿主支援的子集：type/oneOf/properties/required/additionalProperties/items/enum/const
 * 加上 description/title/default/examples。pattern、maxLength、maxItems 之类在输出 schema 里会被拒。
 * @returns 违规清单（空的就是合格）。
 */
export function schemaViolations(node, path = 'schema') {
	if (!isRecord(node)) return [`${path} must be a schema object`];
	const problems = [];
	for (const key of Object.keys(node))
		if (!CONSTRAINT_KEYWORDS.has(key) && !ANNOTATION_KEYWORDS.has(key))
			problems.push(`${path}.${key} is not a supported keyword`);
	for (const key of ['description', 'title'])
		if (Object.hasOwn(node, key) && typeof node[key] !== 'string')
			problems.push(`${path}.${key} must be a string`);
	const hasType = Object.hasOwn(node, 'type');
	const hasOneOf = Object.hasOwn(node, 'oneOf');
	if (hasType && hasOneOf)
		return [...problems, `${path} cannot declare both type and oneOf`];
	if (!hasType && !hasOneOf) {
		for (const key of ONE_OF_SIBLINGS)
			if (Object.hasOwn(node, key))
				problems.push(`${path}.${key} requires type or oneOf`);
		return problems;
	}
	if (hasOneOf) {
		for (const key of ONE_OF_SIBLINGS)
			if (Object.hasOwn(node, key))
				problems.push(`${path}.${key} is not supported beside oneOf`);
		if (!Array.isArray(node.oneOf) || node.oneOf.length < 2)
			return [
				...problems,
				`${path}.oneOf must be an array of at least two schemas`,
			];
		node.oneOf.forEach((child, index) =>
			problems.push(...schemaViolations(child, `${path}.oneOf[${index}]`)),
		);
		return problems;
	}
	const type = node.type;
	if (typeof type !== 'string' || !SCHEMA_TYPES.includes(type))
		return [
			...problems,
			`${path}.type must be one of ${SCHEMA_TYPES.join('/')}`,
		];
	for (const [key, types] of Object.entries(KEYWORD_TYPES))
		if (Object.hasOwn(node, key) && !types.includes(type))
			problems.push(`${path}.${key} is not supported on type "${type}"`);
	if (type === 'object') {
		const properties = node.properties ?? {};
		if (!isRecord(properties))
			problems.push(`${path}.properties must be an object of schemas`);
		else
			for (const [key, child] of Object.entries(properties))
				problems.push(...schemaViolations(child, `${path}.properties.${key}`));
		if (Object.hasOwn(node, 'required')) {
			if (
				!Array.isArray(node.required) ||
				node.required.some((entry) => typeof entry !== 'string')
			)
				problems.push(`${path}.required must be an array of strings`);
			else
				for (const key of node.required)
					if (!isRecord(properties) || !Object.hasOwn(properties, key))
						problems.push(
							`${path}.required names "${key}" which is not in properties`,
						);
		}
		if (
			Object.hasOwn(node, 'additionalProperties') &&
			typeof node.additionalProperties !== 'boolean'
		)
			problems.push(`${path}.additionalProperties must be a boolean`);
	}
	if (type === 'array' && Object.hasOwn(node, 'items'))
		problems.push(...schemaViolations(node.items, `${path}.items`));
	if (SCALAR_TYPES.includes(type)) {
		if (
			Object.hasOwn(node, 'enum') &&
			!(
				Array.isArray(node.enum) &&
				node.enum.length > 0 &&
				node.enum.every((entry) => scalarMatches(type, entry))
			)
		)
			problems.push(`${path}.enum must be a non-empty array of ${type} values`);
		if (Object.hasOwn(node, 'const') && !scalarMatches(type, node.const))
			problems.push(`${path}.const must be a ${type} value`);
	}
	return problems;
}

/** 宿主执行完工具会拿 output.schema 验回传值，不合就当成工具出错。 */
export function valueViolations(schema, value, path = 'value') {
	if (Object.hasOwn(schema, 'oneOf')) {
		const matches = schema.oneOf.filter(
			(child) => valueViolations(child, value, path).length === 0,
		);
		return matches.length === 1
			? []
			: [`${path} must match exactly one oneOf branch`];
	}
	if (!Object.hasOwn(schema, 'type')) return [];
	const type = schema.type;
	if (type === 'object') {
		if (!isRecord(value)) return [`${path} must be an object`];
		const properties = schema.properties ?? {};
		const problems = [];
		for (const key of schema.required ?? [])
			if (!Object.hasOwn(value, key))
				problems.push(`${path}.${key} is required`);
		for (const [key, item] of Object.entries(value)) {
			if (Object.hasOwn(properties, key))
				problems.push(
					...valueViolations(properties[key], item, `${path}.${key}`),
				);
			else if (schema.additionalProperties === false)
				problems.push(`${path}.${key} is not allowed`);
		}
		return problems;
	}
	if (type === 'array') {
		if (!Array.isArray(value)) return [`${path} must be an array`];
		return schema.items
			? value.flatMap((item, index) =>
					valueViolations(schema.items, item, `${path}[${index}]`),
				)
			: [];
	}
	if (!scalarMatches(type, value)) return [`${path} must be ${type}`];
	if (schema.enum && !schema.enum.includes(value))
		return [`${path} must be one of the enum values`];
	if (Object.hasOwn(schema, 'const') && schema.const !== value)
		return [`${path} must equal const`];
	return [];
}

/** tools.register() 的检查；不合格时丢出跟宿主一样的 TypeError。 */
export function assertToolDefinition(definition) {
	const { name, output, timeoutMs } = definition ?? {};
	if (
		output === undefined ||
		typeof output !== 'object' ||
		typeof output.render !== 'function' ||
		(output.presentationMeta !== undefined &&
			typeof output.presentationMeta !== 'function')
	)
		throw new TypeError(
			`tool "${name}" must declare output { schema, render, presentationMeta? }`,
		);
	const problems = schemaViolations(output.schema);
	if (problems.length)
		throw new TypeError(`tool "${name}" ${problems.join('; ')}`);
	if (
		timeoutMs !== undefined &&
		(!Number.isFinite(timeoutMs) || timeoutMs <= 0)
	)
		throw new TypeError(
			`tool "${name}" timeoutMs must be a positive finite number`,
		);
	if (name === RUN_CODE) throw new Error(`tool name "${RUN_CODE}" is reserved`);
	if (typeof definition.execute !== 'function')
		throw new TypeError(`tool "${name}" must have execute()`);
}

/** 宿主处理工具回传值：照 output.schema 验过，再用 render 转成内容块（要能转成 JSON）。 */
export function checkToolResult(definition, args, value) {
	const problems = valueViolations(definition.output.schema, value);
	if (problems.length)
		throw new TypeError(
			`tool "${definition.name}" output: ${problems.join('; ')}`,
		);
	const rendered = definition.output.render(args, value);
	if (
		!Array.isArray(rendered) ||
		rendered.some((block) => !isRecord(block) || typeof block.type !== 'string')
	)
		throw new TypeError(
			`tool "${definition.name}" render must return content blocks`,
		);
	JSON.stringify(rendered);
	return rendered;
}

/**
 * 照宿主规则的工具登记处：register 检查定义、同名重复报错；guard 收下检查函式。
 * scoped(agent) 给会话作用域用的 tools／systemPrompt（restrict 只能点名已登记的全域工具、
 * presentAs 每个作用域只能一次、section／context 的 order 要是有限数、同一作用域同名报错、
 * 要内插的文字里不能有没登记的 {{变数}}）。
 */
export function hostTools() {
	const tools = new Map();
	const guards = [];
	return {
		tools,
		guards,
		register(definition) {
			assertToolDefinition(definition);
			if (tools.has(definition.name))
				throw new Error(`tool "${definition.name}" is already registered`);
			tools.set(definition.name, definition);
			return () => tools.delete(definition.name);
		},
		guard(fn) {
			if (typeof fn !== 'function')
				throw new TypeError('tools.guard() needs a function');
			guards.push(fn);
			return () => guards.splice(guards.indexOf(fn), 1);
		},
		scoped(agent) {
			return scopedHost(agent, tools);
		},
	};
}

function scopedHost(agent, globals) {
	let mode;
	const restrictions = [];
	const sections = new Map();
	const contexts = new Map();
	const prompt = (kind, rows) => (row) => {
		if (!Number.isFinite(row?.order))
			throw new TypeError(
				`prompt ${kind} "${row?.name}" order must be a finite number`,
			);
		if (rows.has(row.name))
			throw new Error(
				`prompt ${kind} "${row.name}" is already registered in this scope`,
			);
		rows.set(row.name, row);
		agent.contexts?.push(row);
		return () => rows.delete(row.name);
	};
	return {
		tools: {
			restrict(filter) {
				const { allow, deny } = filter ?? {};
				if (allow === undefined && deny === undefined)
					throw new Error(
						'tools.restrict({}) is a no-op: pass `allow` and/or `deny`',
					);
				const names = [...(allow ?? []), ...(deny ?? [])];
				if (names.includes(RUN_CODE))
					throw new Error(`tools.restrict() cannot name "${RUN_CODE}"`);
				const unknown = names.filter((name) => !globals.has(name));
				if (unknown.length)
					throw new Error(
						`tools.restrict() names unknown global tools ${unknown.join(', ')}`,
					);
				restrictions.push(filter);
				agent.allowed = allow ?? agent.allowed;
				return () => restrictions.splice(restrictions.indexOf(filter), 1);
			},
			presentAs(next) {
				if (mode !== undefined)
					throw new Error(
						`tools.presentAs("${next}") conflicts with "${mode}" already declared for this scope`,
					);
				mode = next;
				return () => {
					mode = undefined;
				};
			},
			// 这个会话看得到的工具（全域减掉 restrict）。
			visible() {
				return [...globals.keys()].filter((name) =>
					restrictions.every(
						({ allow, deny }) =>
							(allow === undefined || allow.includes(name)) &&
							!(deny ?? []).includes(name),
					),
				);
			},
		},
		systemPrompt: {
			section: prompt('section', sections),
			context: prompt('context', contexts),
			// 宿主组系统提示时做的内插检查：没写 interpolate:false 的段落不能有 {{变数}}（这里没有登记任何变数）。
			assemble() {
				return [...sections.values(), ...contexts.values()].map((row) => {
					const text = typeof row.text === 'function' ? row.text() : row.text;
					if (typeof text !== 'string')
						throw new TypeError(`prompt "${row.name}" text must be a string`);
					if (row.interpolate !== false && /\{\{[^}]*\}\}/u.test(text))
						throw new Error(`unknown prompt variable in "${row.name}"`);
					return text;
				});
			},
		},
		get mode() {
			return mode;
		},
	};
}
