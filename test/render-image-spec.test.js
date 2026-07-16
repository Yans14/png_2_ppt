const test = require('node:test');
const assert = require('node:assert/strict');

const {
  flattenElements,
  gradientXml,
  lineShapeOptions,
  pathPoints,
} = require('../src/render-image-spec');

test('component expansion rotates child geometry around the instance center', () => {
  const spec = {
    components: [{
      id: 'icon',
      elements: [{
        kind: 'shape', id: 'dot', name: 'Dot', layer: 1, group_id: null,
        bounds: { x: 0, y: 0, width: 0.2, height: 0.2 }, rotation_deg: 0,
        preset: 'ellipse', fill: {}, stroke: {}, corner_radius: null,
      }],
    }],
    elements: [{
      kind: 'component', id: 'instance', name: 'Instance', layer: 5, group_id: null,
      bounds: { x: 100, y: 100, width: 100, height: 100 },
      rotation_deg: 90, opacity: 1, component_id: 'icon',
    }],
  };
  const [item] = flattenElements(spec);
  assert.ok(Math.abs(item.bounds.x - 180) < 1e-9);
  assert.ok(Math.abs(item.bounds.y - 100) < 1e-9);
  assert.equal(item.rotation_deg, 90);
});

test('custom path conversion retains native cubic controls', () => {
  const points = pathPoints({
    bounds: { width: 96, height: 192 },
    commands: [
      { op: 'M', x: 0, y: 1, x1: null, y1: null, x2: null, y2: null },
      { op: 'C', x: 1, y: 0, x1: 0.2, y1: 0.8, x2: 0.8, y2: 0.2 },
      { op: 'Z', x: null, y: null, x1: null, y1: null, x2: null, y2: null },
    ],
  });
  assert.deepEqual(points[0], { x: 0, y: 2, moveTo: true });
  assert.equal(points[1].curve.type, 'cubic');
  assert.equal(points[1].curve.x1, 0.2);
  assert.equal(points[1].curve.y2, 0.4);
  assert.deepEqual(points[2], { close: true });
});

test('gradient XML writes PowerPoint alpha values', () => {
  const xml = gradientXml({
    opacity: 0.5,
    angle_deg: 45,
    stops: [
      { position: 0, color: '#112233', opacity: 0.4 },
      { position: 1, color: '#445566', opacity: 1 },
    ],
  });
  assert.match(xml, /<a:alpha val="20000"\/>/);
  assert.match(xml, /<a:alpha val="50000"\/>/);
  assert.match(xml, /ang="2700000"/);
});

test('reversed line geometry uses non-negative extents and PowerPoint flips', () => {
  assert.deepEqual(
    lineShapeOptions({ x1: 96, y1: 192, x2: 0, y2: 96 }),
    { x: 0, y: 1, w: 1, h: 1, flipH: true, flipV: true },
  );
  assert.deepEqual(
    lineShapeOptions({ x1: 0, y1: 192, x2: 96, y2: 96 }),
    { x: 0, y: 1, w: 1, h: 1, flipH: false, flipV: true },
  );
});
