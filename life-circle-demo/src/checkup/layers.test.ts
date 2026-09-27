/**
 * 图层映射测三件事：三种形态各自画成什么、点按什么着色、以及"没加载"与"加载了但为空"
 * 有没有被分开。
 */
import { describe, expect, it } from 'vitest';
import { LAYER_STYLES, drawableLayer, serviceSamples } from './layers';
import { collection, feature, layer, point, polygon } from './fixtures';

describe('layer states', () => {
  it('separates "not fetched" from "fetched and empty"', () => {
    expect(drawableLayer('service_gaps', undefined)).toMatchObject({ state: 'absent', shapes: [],
      points: [] });
    expect(drawableLayer('service_gaps', layer({ geometry: collection([]) })))
      .toMatchObject({ state: 'empty' });
    // 没有形状也没有文档：合法的"这次没有内容"，不是错误。
    expect(drawableLayer('isochrone', layer({ layerId: 'isochrone', geometry: null,
      displayGeometry: null }))).toMatchObject({ state: 'empty' });
  });

  it('refuses to guess at a shape it cannot parse', () => {
    // 客户端取回时已经校验过；这里是第二道闸，宁可报错也不画一个认不出来的形状。
    expect(() => drawableLayer('service_gaps', layer({ geometry: { kind: 'zones' } })))
      .toThrow(/几何无法解析/);
  });
});

describe('polygon layers', () => {
  it('draws the boundary as one shape with its own style', () => {
    const drawable = drawableLayer('isochrone', layer({ layerId: 'isochrone',
      geometry: polygon() }));
    expect(drawable.state).toBe('ready');
    expect(drawable.shapes).toHaveLength(1);
    expect(drawable.shapes[0].style).toBe(LAYER_STYLES.isochrone);
    expect(drawable.shapes[0].geometry.type).toBe('Polygon');
  });
});

describe('feature collection layers', () => {
  it('colours facilities by major category and keys them by id', () => {
    const facilities = layer({ layerId: 'facilities', displayGeometry: null,
      geometry: collection([
        feature(point(116.4, 39.9), { id: 'f-1', name: '青禾菜市场', majorCategory: 'shopping' }),
        feature(point(116.41, 39.91), { id: 'f-2', name: '社区药房', majorCategory: 'medical' }),
      ]) });
    const drawable = drawableLayer('facilities', facilities);
    expect(drawable.points.map(item => [item.key, item.color]))
      .toEqual([['f-1', '#168875'], ['f-2', '#397ac6']]);
    expect(drawable.points[0].title).toContain('青禾菜市场');
    expect(drawable.shapes).toEqual([]);
  });

  it('never invents a category colour for a point that has none', () => {
    const drawable = drawableLayer('facilities', layer({ layerId: 'facilities',
      displayGeometry: null, geometry: collection([feature(point(116.4, 39.9), { id: 'x' })]) }));
    expect(drawable.points[0].color).toBe(LAYER_STYLES.facilities.fillColor);
    expect(drawable.points[0].title).toContain('未分类');
  });

  it('keeps one shape per grey zone and marks the ones whose query never finished', () => {
    const gaps = layer({ displayGeometry: null, geometry: collection([
      feature(polygon(), { id: 'zone-0', queryStatus: 'complete' }),
      feature(polygon(0.01), { id: 'zone-1', queryStatus: 'partial' }),
    ]) });
    const drawable = drawableLayer('service_gaps', gaps);
    expect(drawable.shapes.map(shape => shape.key)).toEqual(['zone-0', 'zone-1']);
    // 检索没跑完的灰区不是同一种结论：它可能是目录漏采，颜色必须看得出来。
    expect(drawable.shapes[0].style.fillColor).toBe(LAYER_STYLES.service_gaps.fillColor);
    expect(drawable.shapes[1].style.fillColor).toBe('#b54708');
  });

  it('colours verification points by what the route actually said', () => {
    const verification = layer({ layerId: 'verification', displayGeometry: null,
      geometry: collection([
        feature(point(116.4, 39.9), { facilityId: 'f-1', poiStatus: 'verified_reachable' }),
        feature(point(116.41, 39.91), { facilityId: 'f-2', poiStatus: 'verified_unreachable' }),
      ]) });
    const drawable = drawableLayer('verification', verification);
    expect(drawable.points.map(item => item.color)).toEqual(['#147d70', '#b54708']);
    expect(drawable.points[1].title).toContain('f-2');
  });

  it('shows model distances and keeps distinct categories at the same cell', () => {
    const heat = layer({ layerId: 'heatmap', displayGeometry: null, geometry: collection([
      feature(point(116.4, 39.9), { cell: '0:1:2', category: 'medical', distanceM: 320.4,
        status: 'covered' }),
      feature(point(116.4, 39.9), { cell: '0:1:2', category: 'shopping', distanceM: 900,
        status: 'unknown' }),
    ]) });
    expect(drawableLayer('heatmap', heat).points[0].title).toContain('320 米');
    expect(new Set(drawableLayer('heatmap', heat).points.map(p => p.key)).size).toBe(2);
  });
});

describe('service coverage samples', () => {
  it('reads the cell size from the level and the grid step, and keeps the domain', () => {
    const heat = layer({ layerId: 'heatmap', displayGeometry: null, geometry: collection([
      feature(point(116.4, 39.9), { cell: '0:1:2', category: 'medical', distanceM: 320.4, status: 'covered' }),
      feature(point(116.4001, 39.9), { cell: '1:3:4', category: 'shopping', distanceM: 900, status: 'unknown' }),
      feature(point(116.4002, 39.9), { cell: '2:9:9', category: 'education', status: 'gap' }),
    ], { stepM: 40, domain: polygon() }) });
    const { samples, domain, dropped } = serviceSamples(heat);
    expect(samples.map(sample => sample.sizeM)).toEqual([40, 20, 10]);
    expect(samples[1]).toMatchObject({ category: 'shopping', status: 'unknown', distanceM: 900 });
    // 没有距离就是没有：不补 0，不补"很远"。
    expect(samples[2].distanceM).toBeNull();
    expect(domain).toEqual(polygon());
    expect(dropped).toBe(0);
  });

  it('drops cells it cannot read and says how many, instead of guessing', () => {
    const heat = layer({ layerId: 'heatmap', displayGeometry: null, geometry: collection([
      feature(point(116.4, 39.9), { cell: 'x:1:2', category: 'medical', status: 'covered', distanceM: 1 }),
      feature(point(116.4, 39.9), { cell: '0:1:2', category: 'medical', status: 'maybe' }),
      feature(point(116.4, 39.9), { cell: '0:1:2', status: 'gap' }),
      feature(point(116.4, 39.9), { cell: '0:1:3', category: 'medical', status: 'gap' }),
    ]) });
    const result = serviceSamples(heat);
    expect(result.dropped).toBe(3);
    // 图层没报步长时按后端的基准步长 50 米；没有评估域就不做域内未知。
    expect(result.samples).toEqual([expect.objectContaining({ sizeM: 50, status: 'gap' })]);
    expect(result.domain).toBeNull();
  });

  it('has nothing to draw before the layer is fetched', () => {
    expect(serviceSamples(undefined)).toEqual({ samples: [], domain: null, dropped: 0 });
  });
});

describe('the document layer', () => {
  it('draws nothing and says so, because the report is not a shape', () => {
    const drawable = drawableLayer('report', layer({ layerId: 'report', geometry: null,
      displayGeometry: null, document: { reportId: 'task-1:5' } }));
    expect(drawable).toMatchObject({ state: 'ready', shapes: [], points: [] });
  });
});

describe('styles', () => {
  it('gives every layer a note, so the legend never has a colour without a meaning', () => {
    for (const [id, style] of Object.entries(LAYER_STYLES)) {
      expect(style.note.length, id).toBeGreaterThan(0);
      expect(style.label.length, id).toBeGreaterThan(0);
    }
    // 评估域只有边界、没有面积含义：给它填色会被读成"这一片也是覆盖的"。
    expect(LAYER_STYLES.accessibility.fillOpacity).toBe(0);
  });
});
