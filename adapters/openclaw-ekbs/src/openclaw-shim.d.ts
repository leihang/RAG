/** 本仓库可不装 openclaw；仅提供编译期模块名。 */
declare module "openclaw/plugin-sdk/plugin-entry" {
  export function definePluginEntry(def: {
    id: string
    name: string
    description?: string
    register: (api: unknown) => void | Promise<void>
  }): unknown
}
