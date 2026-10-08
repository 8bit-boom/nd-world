import scene from "./scene.json";
type Loose<T> = T extends number ? number : T extends string ? string : T extends boolean ? boolean : T extends readonly (infer U)[] ? Loose<U>[] : T extends object ? { [K in keyof T]: Loose<T[K]> } : T;
type Known<Obj, Schema> = { [K in keyof Obj]: K extends keyof Schema ? true : never };   // every key the exporter writes must exist in the schema
type Wall = foundry.documents.BaseWall.CreateData;
type Light = foundry.documents.BaseAmbientLight.CreateData;
type Scene = foundry.documents.BaseScene.CreateData;

const walls: Loose<Wall>[] = scene.walls;
const lights: Loose<Light>[] = scene.lights;
const wallKeys: Known<(typeof scene.walls)[number], Wall> = null as any as { [K in keyof (typeof scene.walls)[number]]: true };
const lightKeys: Known<(typeof scene.lights)[number], Light> = null as any as { [K in keyof (typeof scene.lights)[number]]: true };
const sceneTop: Known<Omit<typeof scene, "walls" | "lights">, Scene> = null as any as { [K in keyof Omit<typeof scene, "walls" | "lights">]: true };
const gridKeys: Known<typeof scene.grid, NonNullable<Scene["grid"]>> = null as any as { [K in keyof typeof scene.grid]: true };
export { walls, lights, wallKeys, lightKeys, sceneTop, gridKeys };
