import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import MarkdownDocument, { isMarkdownFile } from './MarkdownDocument';
import { SkillDocument, SkillEditor } from './SkillsPage';
import type { Skill } from './types';

const instructions = '# 质量核对\n\n1. 读取记录\n2. **核对数量**\n\n| 项目 | 数量 |\n| --- | --- |\n| 订单 | 12 |\n\n```json\n{"checked": true}\n```';
const skill: Skill = { id: 'quality', name: '质量核对', description: '按批次核对数量', instructions, enabled: true, revision: 2, required_tools: ['read_quality'], resources: { '核对说明.md': '## 口径\n\n按业务日期统计。' }, scope: 'team', can_edit: false };

describe('readable Markdown documents', () => {
  it('renders headings lists tables and code by default instead of a plain text block', () => {
    const html = renderToStaticMarkup(<MarkdownDocument text={instructions}/>);
    for (const markup of ['<h1>质量核对</h1>', '<ol>', '<strong>核对数量</strong>', '<table>', 'language-json']) expect(html).toContain(markup);
    expect(html).not.toContain('<textarea'); expect(html).toContain('aria-pressed="true">阅读');
  });
  it('keeps scripts HTML dangerous links and remote image tracking inert', () => {
    const html = renderToStaticMarkup(<MarkdownDocument text={'<script>alert(1)</script>\n\n[点击](javascript:alert%281%29)\n\n![追踪图](https://external.invalid/pixel.png)'}/>);
    expect(html).not.toContain('<script>'); expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('<img'); expect(html).toContain('外部图片已阻止自动加载');
  });
  it('exposes unmodified Markdown in the editable source view', () => {
    const html = renderToStaticMarkup(<MarkdownDocument text={instructions} onChange={() => {}} initialView="source" label="流程指引"/>);
    expect(html).toContain('<textarea'); expect(html).toContain('# 质量核对'); expect(html).toContain('**核对数量**');
    expect(html).toContain('源码编辑'); expect(html).not.toContain('<h1>质量核对</h1>');
  });
  it('detects Markdown MIME types and text uploads by filename', () => {
    expect(isMarkdownFile({ name: 'notes', mime_type: 'text/markdown; charset=utf-8' })).toBe(true);
    expect(isMarkdownFile({ name: 'SKILL.MD', mime_type: 'text/plain' })).toBe(true);
    expect(isMarkdownFile({ name: 'notes.txt', mime_type: 'text/plain' })).toBe(false);
  });
});

describe('Skill details respect document and resource permissions', () => {
  it('reads instructions and references with the same renderer while retaining version and tool names', () => {
    const before = JSON.stringify(skill);
    const html = renderToStaticMarkup(<SkillDocument value={skill}/>);
    expect(html).toContain('<h1>质量核对</h1>'); expect(html).toContain('<h2>口径</h2>');
    expect(html).toContain('v2'); expect(html).toContain('read_quality'); expect(html).not.toContain('配置助手');
    expect(html).toContain('<table>'); expect(html).toContain('language-json');
    expect(html).toContain('aria-label="工具与参考资料"'); expect(html).toContain('aria-label="参考资料"');
    expect(JSON.stringify(skill)).toBe(before);
  });
  it('keeps each reference independently readable and applies the same safety rules to reference text and filenames', () => {
    const value = { ...skill, resources: {
      '口径.md': '## 统计口径\n\n> 按业务日期汇总。\n\n- [x] 核对日期\n- [ ] 核对数量',
      '<img src=x onerror=alert(1)>.md': '<script>alert(1)</script>\n\n[执行](javascript:alert%281%29)\n\n![外部图](https://external.invalid/track.png)',
    } };
    const html = renderToStaticMarkup(<SkillDocument value={value}/>);
    expect(html.match(/<details class="skill-reference">/g)).toHaveLength(2);
    expect(html).toContain('<blockquote>'); expect(html).toContain('type="checkbox"');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;.md');
    expect(html).not.toContain('<script>'); expect(html).not.toContain('<img'); expect(html).not.toContain('href="javascript:');
    expect(html).toContain('外部图片已阻止自动加载');
  });
  it('opens existing authorized details in reading mode and keeps unauthorized resources read only', () => {
    const denied = renderToStaticMarkup(<SkillEditor value={skill} admin={true} onClose={() => {}} onSaved={() => {}}/>);
    expect(denied).toContain('<h1>质量核对</h1>'); expect(denied).not.toContain('<textarea'); expect(denied).not.toContain('保存版本');
    const allowed = renderToStaticMarkup(<SkillEditor value={{ ...skill, can_edit: true }} admin={true} onClose={() => {}} onSaved={() => {}}/>);
    expect(allowed).toContain('<h1>质量核对</h1>'); expect(allowed).toContain('编辑</button>'); expect(allowed).not.toContain('<textarea');
  });
});
