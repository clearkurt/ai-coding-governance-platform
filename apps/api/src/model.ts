export interface Generation { analysis: string; suggestion: string; code: string; cautions: string[]; }
export interface ModelProvider { generate(input: { requirement: string; codeStyle: string; sources: Array<{name:string;content:string}> }): Promise<Generation>; }
export class MockModelProvider implements ModelProvider {
  async generate(input: { requirement: string; codeStyle: string; sources: Array<{name:string;content:string}> }) {
    await new Promise(resolve => setTimeout(resolve, 650));
    const context = input.sources.length ? `已分析 ${input.sources.map(f => f.name).join('、')}。` : '未提供现有源码，以下为独立草案。';
    return { analysis: `${context} 需求为：${input.requirement}`, suggestion: '建议先在目标板上逐段验证 LCD 映射，再合入主工程。', code: `/** ${input.requirement} */\nvoid lcd_update_display(void)\n{\n    /* TODO: 根据已确认的 SEG/COM 映射写入 LCD RAM。 */\n}\n`, cautions: ['请确认芯片型号与 LCD COM 数。', '请在真机验证每个符号和数字笔段。'] };
  }
}
