import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { BusinessIcon, businessIconFor } from './BusinessIcon';

describe('Flint business icons', () => {
  it.each([
    ['公司方案解读', 'source-trace'], ['工业异常分析', 'quality-inspect'], ['有据报告', 'reports'],
    ['查工单', 'work-order-search'], ['看库存', 'inventory'], ['看排产', 'production-schedule'],
    ['查领料', 'material-pick'], ['看设备', 'equipment-status'], ['看告警', 'alerts'],
    ['getSalesOrders', 'sales-orders'], ['待审批', 'approval'], ['连接数据源', 'data-connect'],
  ])('maps %s to the corresponding artwork', (label, icon) => {
    expect(businessIconFor(label)).toBe(icon);
  });
  it('prioritizes the name, permits a description fallback, and leaves unrelated cards neutral', () => {
    expect(businessIconFor('有据报告', '查询库存')).toBe('reports');
    expect(businessIconFor('指标助手', '整理库存')).toBe('inventory');
    expect(businessIconFor('写邮件')).toBeUndefined();
  });
  it('uses matching accent and monochrome originals without duplicating the accessible label', () => {
    for (const mono of [false, true]) {
      const html = renderToStaticMarkup(<BusinessIcon icon="inventory" mono={mono} />);
      expect(html).toContain('brand/business/' + (mono ? 'mono/' : '') + 'inventory.svg');
      expect(html).toContain('alt=""');
    }
  });
});
