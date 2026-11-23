// 假的 opencode CLI（测试用，不连任何网路）：用法 node fake-opencode.mjs run -m M --agent A --format json "<user>"
// 环境变数 FAKE_OPENCODE：ok（预设，回一段事件流）｜hang（卡住不结束）｜hang-child（先起一个会一直活着的子进程，把它的 pid 写进 FAKE_PIDFILE，再卡住）｜exit1｜empty｜garbage
// 用来测执行器的真实 spawn 路径：逾时、非 0 结束码、空回答、事件流解析，以及「逾时会不会连子进程一起杀掉」。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const mode = process.env.FAKE_OPENCODE ?? 'ok';
const user = process.argv.at(-1);
const emit = (event) => process.stdout.write(JSON.stringify(event) + '\n');

if (mode === 'hang') setInterval(() => {}, 1000);
else if (mode === 'hang-child') {
	const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
	writeFileSync(process.env.FAKE_PIDFILE, String(child.pid));
	setInterval(() => {}, 1000);
} else if (mode === 'exit1') {
	process.stderr.write('simulated crash\n');
	process.exit(1);
} else if (mode === 'empty') {
	emit({ type: 'step_start' });
	emit({ type: 'step_finish', part: { tokens: { input: 1, output: 0 } } });
} else if (mode === 'garbage') {
	process.stdout.write('{"type":"text","part":{"text":"半句\n');
} else {
	emit({ type: 'step_start' });
	emit({ type: 'text', part: { text: process.env.FAKE_ANSWER ?? `收到：${user}` } });
	emit({ type: 'step_finish', part: { tokens: { input: 7200, output: 8 } } });
}
