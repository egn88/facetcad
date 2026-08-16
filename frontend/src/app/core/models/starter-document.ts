/**
 * A worked example: a parametric plate with a pocket, driven entirely by the
 * sheet. Every dimension traces back to a parameter, so widening `plate_w`
 * moves the pocket with it and no face changes its name.
 */
export const STARTER_DOCUMENT = {
  schema: "cadsheet/1",
  project: "plate",
  parameters: [
    { name: "plate_w", value: 120, group: "Plate", doc: "overall width" },
    { name: "plate_h", expr: "plate_w * 0.6", group: "Plate" },
    { name: "plate_t", value: 6, group: "Plate" },
    { name: "slot_w", value: 30, group: "Slot" },
    { name: "slot_d", value: 2, group: "Slot" },
  ],
  datums: {
    base: { type: "plane", origin: [0, 0, 0], normal: [0, 0, 1] },
    top: { type: "plane", origin: [0, 0, "plate_t"], normal: [0, 0, 1] },
  },
  sketches: {
    outline: {
      plane: "base",
      points: {
        p0: [0, 0],
        p1: ["plate_w", 0],
        p2: ["plate_w", "plate_h"],
        p3: [0, "plate_h"],
      },
      curves: [
        { id: "bottom", start: "p0", end: "p1" },
        { id: "right", start: "p1", end: "p2" },
        { id: "top", start: "p2", end: "p3" },
        { id: "left", start: "p3", end: "p0" },
      ],
      loops: [{ id: "outer", curves: ["bottom", "right", "top", "left"] }],
    },
    hole: {
      plane: "top",
      points: {
        q0: ["plate_w / 2 - slot_w / 2", "plate_h / 2 - slot_w / 2"],
        q1: ["plate_w / 2 + slot_w / 2", "plate_h / 2 - slot_w / 2"],
        q2: ["plate_w / 2 + slot_w / 2", "plate_h / 2 + slot_w / 2"],
        q3: ["plate_w / 2 - slot_w / 2", "plate_h / 2 + slot_w / 2"],
      },
      curves: [
        { id: "c0", start: "q0", end: "q1" },
        { id: "c1", start: "q1", end: "q2" },
        { id: "c2", start: "q2", end: "q3" },
        { id: "c3", start: "q3", end: "q0" },
      ],
      loops: [{ id: "outer", curves: ["c0", "c1", "c2", "c3"] }],
    },
  },
  features: [
    { id: "base", type: "pad", profile: "outline.outer", length: "plate_t" },
    {
      id: "slot",
      type: "pocket",
      profile: "hole.outer",
      depth: "slot_d",
      direction: "-normal",
    },
  ],
};
