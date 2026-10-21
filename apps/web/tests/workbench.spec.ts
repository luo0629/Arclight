import { test, expect } from '@playwright/test';

test('真实 API：注册、创建项目剧集、双审核、画布保存、人工新版本与刷新登录', async ({ page }) => {
  const crashes: string[] = []; page.on('pageerror', error => crashes.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '第一次来？创建账号' }).click();
  await page.getByLabel('怎么称呼你').fill('小星星');
  await page.getByLabel('邮箱', { exact: true }).fill(`creator-${Date.now()}@example.com`);
  await page.getByLabel('密码', { exact: true }).fill('arclight-test-password');
  await page.getByRole('button', { name: '注册并开始创作' }).click();
  await page.getByRole('button', { name: '创建第一个项目' }).click();
  await expect(page.getByLabel('项目名称')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button', { name: '创建项目', exact: true })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('项目名称')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '创建第一个项目' })).toBeFocused();
  await page.getByRole('button', { name: '创建第一个项目' }).click();
  await page.getByLabel('项目名称').fill('小猫的雨天手帐');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await page.getByRole('button', { name: '添加第一集' }).click();
  await page.getByLabel('剧集标题').fill('一把会唱歌的伞');
  await page.getByRole('button', { name: '创建剧集', exact: true }).click();
  await expect(page.getByRole('heading', { name: '第 1 集 · 一把会唱歌的伞' })).toBeVisible();
  await page.getByLabel('故事灵感').fill('雨夜里，小猫把一把会唱歌的伞借给迷路的小女孩，他们一起寻找星星。');
  await page.getByRole('button', { name: '请编剧开始创作' }).click();
  await expect(page.getByText('等待审核：剧本版本', { exact: false })).toBeVisible({ timeout: 20000 });
  await page.getByLabel('审核意见').fill('故事方向很好，继续制作分镜。');
  await page.getByRole('button', { name: '批准并继续' }).click();
  await expect(page.getByText('等待审核：分镜版本', { exact: false })).toBeVisible({ timeout: 20000 });
  await page.getByRole('button', { name: '批准并继续' }).click();
  await expect(page.locator('.task .status')).toHaveText('已完成', { timeout: 20000 });
  await page.getByRole('button', { name: '创作画布', exact: true }).click();
  await expect(page.locator('.paper-node.script')).toBeVisible();
  await expect(page.locator('.paper-node.storyboard')).toBeVisible();
  const node = page.locator('.paper-node.script'); const box = await node.boundingBox();
  if (!box) throw new Error('剧本节点未渲染');
  await page.mouse.move(box.x + 50, box.y + 35); await page.mouse.down(); await page.mouse.move(box.x + 80, box.y + 75, { steps: 10 }); await page.mouse.up();
  await page.getByRole('button', { name: '保存画布位置' }).click();
  await expect(page.getByText('画布位置已保存', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '版本列表', exact: true }).click();
  await page.locator('.version-row').filter({ hasText: '版本 1' }).first().click();
  await page.getByRole('button', { name: '编辑并另存新版本' }).click();
  await page.getByLabel('剧本标题').fill('雨天里的星星');
  await page.getByRole('button', { name: '保存为新版本', exact: true }).click();
  await expect(page.locator('.version-row').filter({ hasText: '雨天里的星星' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '第 1 集 · 一把会唱歌的伞' })).toBeVisible();
  await page.getByRole('button', { name: '版本列表', exact: true }).click();
  await page.locator('.version-row').filter({ hasText: '雨天里的星星' }).click();
  await page.getByRole('button', { name: '批准该版本', exact: true }).click();
  await expect(page.locator('.inspector .section-heading .status')).toHaveText('已批准');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel('故事灵感')).not.toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'test-results/mobile-workbench.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '创作画布', exact: true }).click();
  await page.screenshot({ path: 'test-results/desktop-workbench.png', fullPage: true });
  expect(crashes).toEqual([]);
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来，小创作家' })).toBeVisible();
});


test('真实 API：成员角色、只读权限、归档恢复与移除成员', async ({ page, browser, baseURL }) => {
  const stamp = Date.now();
  const password = 'arclight-collaboration-test';
  const ownerResponse = await page.request.post('/api/v1/auth/register', { data: {
    email: `owner-${stamp}@example.com`, password, display_name: '手帐主人',
  } });
  expect(ownerResponse.status()).toBe(201);
  const owner = await ownerResponse.json();
  const headers = { Authorization: `Bearer ${owner.access_token}` };
  const projectResponse = await page.request.post('/api/v1/projects', { headers, data: { name: '伙伴们的故事本' } });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
    headers, data: { episode_no: 1, title: '一起画星星' },
  });
  expect(episodeResponse.status()).toBe(201);
  const memberContext = await browser.newContext({ baseURL });
  try {
    const memberEmail = `partner-${stamp}@example.com`;
    const memberResponse = await memberContext.request.post('/api/v1/auth/register', { data: {
      email: memberEmail, password, display_name: '小伙伴',
    } });
    expect(memberResponse.status()).toBe(201);
    const member = await memberResponse.json();
    await page.goto('/');
    await page.getByRole('button', { name: '创作伙伴', exact: true }).click();
    await page.getByLabel('伙伴的邮箱').fill(memberEmail);
    await page.getByLabel('项目角色').selectOption('viewer');
    await page.getByRole('button', { name: '添加成员', exact: true }).click();
    await expect(page.getByLabel('小伙伴的角色')).toHaveValue('viewer');
    const partner = await memberContext.newPage();
    await partner.goto('/');
    await expect(partner.getByText('你当前拥有只读权限', { exact: true })).toBeVisible();
    await expect(partner.getByRole('button', { name: '＋ 添加一集', exact: true })).toHaveCount(0);
    await expect(partner.getByRole('button', { name: '项目设置与归档', exact: true })).toHaveCount(0);
    const denied = await memberContext.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers: { Authorization: `Bearer ${member.access_token}` }, data: { episode_no: 2, title: '不应创建' },
    });
    expect(denied.status()).toBe(403);
    await page.getByLabel('小伙伴的角色').selectOption('editor');
    await expect(page.getByLabel('小伙伴的角色')).toBeEnabled();
    await partner.reload();
    await expect(partner.getByText('你可以编辑与创建任务', { exact: true })).toBeVisible();
    await expect(partner.getByRole('button', { name: '＋ 添加一集', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '项目设置与归档', exact: true }).click();
    await page.getByLabel('项目名称').fill('星星创作小队');
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await expect(page.getByText('设置已保存', { exact: true })).toBeVisible();
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: '归档项目', exact: true }).click();
    await expect(page.getByRole('button', { name: '恢复项目', exact: true })).toBeVisible();
    await expect(page.getByLabel('项目名称')).toBeDisabled();
    await partner.reload();
    await expect(partner.getByRole('button', { name: '＋ 添加一集', exact: true })).toHaveCount(0);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: '恢复项目', exact: true }).click();
    await expect(page.getByLabel('项目名称')).toBeEnabled();
    await page.getByRole('button', { name: '创作伙伴', exact: true }).click();
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: '移除', exact: true }).click();
    await expect(page.getByLabel('小伙伴的角色')).toHaveCount(0);
    const hidden = await memberContext.request.get(`/api/v1/projects/${project.id}`, {
      headers: { Authorization: `Bearer ${member.access_token}` },
    });
    expect(hidden.status()).toBe(404);
    await partner.reload();
    await expect(partner.getByRole('heading', { name: '新的一页，等你落笔' })).toBeVisible();
  } finally { await memberContext.close(); }
});
