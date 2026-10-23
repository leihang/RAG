/** 个人库在界面上要同时看到姓名、用户编号和钉钉 ID，不能只剩 personal-5 / p5。 */

export interface PersonalOwner {
  owner_user_id?: number | null
  owner_nick?: string | null
  owner_dingtalk_userid?: string | null
}

export function personalOwnerText(row: PersonalOwner): string | null {
  if (!row.owner_user_id) return null
  const name = row.owner_nick?.trim() || '未命名'
  const ding = row.owner_dingtalk_userid ? `，钉钉 ${row.owner_dingtalk_userid}` : ''
  return `${name}（用户 ${row.owner_user_id}${ding}）`
}
