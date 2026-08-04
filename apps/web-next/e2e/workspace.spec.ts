import { expect, test } from "@playwright/test";

test("loads resources, runs a task, and offers rollback", async ({ page }) => {
  await page.route("**/auth/me", (route) =>
    route.fulfill({
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        team_id: "22222222-2222-4222-8222-222222222222",
        email: "member@example.com",
      },
    }),
  );
  await page.route("**/devices", (route) =>
    route.fulfill({
      json: [
        {
          id: "33333333-3333-4333-8333-333333333333",
          name: "Windows Lab",
          runtime_version: "company-agent/0.1.0",
          last_seen_at: new Date().toISOString(),
          online: true,
          projects: [
            {
              id: "44444444-4444-4444-8444-444444444444",
              device_id: "33333333-3333-4333-8333-333333333333",
              root_id: "root-1",
              display_name: "Firmware",
            },
          ],
        },
      ],
    }),
  );
  await page.route("**/conversations", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        json: { id: "66666666-6666-4666-8666-666666666666", title: "New", created_at: "now" },
      });
      return;
    }
    await route.fulfill({
      json: [
        {
          id: "55555555-5555-4555-8555-555555555555",
          title: "Existing task",
          created_at: "now",
        },
      ],
    });
  });
  await page.route("**/pairing-codes", (route) =>
    route.fulfill({ json: { code: "pair-code", expires_at: "2026-08-04T16:00:00Z" } }),
  );
  await page.route("**/tasks", (route) =>
    route.fulfill({
      status: 201,
      json: { id: "77777777-7777-4777-8777-777777777777", root_id: "root-1", status: "pending" },
    }),
  );
  await page.route("**/tasks/77777777-7777-4777-8777-777777777777/events", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: 'id: 1\nevent: turn/completed\ndata: {"sequence":1,"payload":{"summary":"done"}}\n\n',
    }),
  );
  await page.route("**/tasks/77777777-7777-4777-8777-777777777777/rollback", (route) =>
    route.fulfill({
      json: {
        task_id: "77777777-7777-4777-8777-777777777777",
        status: "requested",
        delivery_id: "rollback-1",
        created: true,
      },
    }),
  );

  await page.goto("/");
  await expect(page.getByText("Windows Lab（在线）")).toBeVisible();
  await expect(page.getByText("Firmware")).toBeVisible();
  await expect(page.getByText("Existing task")).toBeVisible();

  await page.locator("textarea").fill("修复状态机");
  await page.getByRole("button", { name: "开始执行" }).click();
  await expect(page.getByText("completed", { exact: true })).toBeVisible();
  await expect(page.getByText("turn/completed")).toBeVisible();

  await page.getByRole("button", { name: "回滚本次修改" }).click();
  await expect(page.getByText("回滚状态：requested")).toBeVisible();

  await page.getByRole("button", { name: "生成设备配对码" }).click();
  await expect(page.getByText(/配对码：pair-code/)).toBeVisible();
});
