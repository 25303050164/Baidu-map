import { describe, it, expect } from 'vitest';
import { classifyPoi, normalizeName } from './classify';

describe('facility classification', () => {
  it('maps the three P0 classes to sub-category and legacy category', () => {
    expect(classifyPoi({ name: '青禾菜市场' })).toMatchObject({ major: 'shopping', minor: 'wet-market', category: 'market' });
    expect(classifyPoi({ name: '康宁大药房' })).toMatchObject({ major: 'medical', minor: 'pharmacy', category: 'pharmacy' });
    expect(classifyPoi({ name: '青禾实验小学' })).toMatchObject({ major: 'education', minor: 'primary-school', category: 'school' });
  });

  it('covers the coarse major categories beyond the three P0 classes', () => {
    expect(classifyPoi({ name: '市第一人民医院' })).toMatchObject({ major: 'medical', minor: 'hospital' });
    expect(classifyPoi({ name: '幸福养老院' })).toMatchObject({ major: 'care', minor: 'nursing-home' });
    expect(classifyPoi({ name: '老街面馆' })).toMatchObject({ major: 'dining', minor: 'restaurant' });
    expect(classifyPoi({ name: '工商银行' })).toMatchObject({ major: 'finance', minor: 'bank' });
    expect(classifyPoi({ name: '青禾公园' })).toMatchObject({ major: 'leisure', minor: 'park' });
    expect(classifyPoi({ name: '公交车站' })).toMatchObject({ major: 'transport', minor: 'bus' });
  });

  it('excludes training institutions and other obvious misclasses', () => {
    for (const name of ['新东方英语培训中心', '学而思辅导班', '青禾美术兴趣班', '某某教育科技']) {
      const result = classifyPoi({ name });
      expect(result.category).toBeUndefined();
      if (result.minor === 'training') expect(result.negative).toBe(true);
      else expect(result.major).toBeUndefined();
    }
    expect(classifyPoi({ name: '青禾花鸟市场' }).major).toBeUndefined();
    expect(classifyPoi({ name: '宠物药店' }).major).toBeUndefined();
  });

  it('honours boundary flags for the still-unconfirmed cases', () => {
    expect(classifyPoi({ name: '河畔生鲜超市' })).toMatchObject({ major: 'shopping', minor: 'fresh-store', category: 'market' });
    expect(classifyPoi({ name: '河畔生鲜超市' }, { freshMarket: false, hospitalPharmacy: true, combinedSchool: true }).minor).toBe('supermarket');
    expect(classifyPoi({ name: '人民医院药房' })).toMatchObject({ minor: 'hospital-pharmacy', category: 'pharmacy' });
    expect(classifyPoi({ name: '人民医院药房' }, { freshMarket: true, hospitalPharmacy: false, combinedSchool: true }).minor).toBe('pharmacy');
    expect(classifyPoi({ name: '南苑完小' })).toMatchObject({ minor: 'combined-school', category: 'school' });
    expect(classifyPoi({ name: '南苑完小' }, { freshMarket: true, hospitalPharmacy: true, combinedSchool: false }).major).toBeUndefined();
  });

  it('normalizes full-width, spaces and punctuation', () => {
    expect(normalizeName('ＡＢＣ　小学')).toBe('abc小学');
    expect(normalizeName('青禾·菜市场（旗舰店）')).toBe('青禾菜市场旗舰店');
  });
});
