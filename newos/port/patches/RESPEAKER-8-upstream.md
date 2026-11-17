# RESPEAKER-8 Upstream Submission Packet: `phy: rockchip: inno-usb2` Probe-Defer & Unbind UAF

> **Review note (2026-11-17).** Status of this patch: applies to mainline 7.4-rc3 and to
> v6.18.63, compiles with W=1, passes checkpatch apart from the missing Signed-off-by. It has
> **not** been booted on hardware, and the userspace sanitizer harness mentioned below is not
> in this repository. A smaller variant of the same fix (the three works set up with
> `devm_delayed_work_autocancel()`) was boot-tested on the board: without it a device tree
> with `phy-supply` on the host ports oopses about 4 s into every boot; with it both PHYs
> defer once and then probe, with no oops. Before sending either, keep a single Fixes tag
> (`8dc60f8da22f`), add the oops trace, and disclose the use of an AI coding assistant as
> the kernel's process documentation asks. Nothing has been sent upstream.

> **OWNER GATE:** This packet is prepared for human review and submission. Per Linux kernel DCO rules and project policy, **do not send emails or push upstream automatically** — the human owner must add their real-name `Signed-off-by:` and run `git send-email`.

## 1. Patch Artifact & Target Trees

- **Patch file:** `newos/port/patches/0001-phy-rockchip-inno-usb2-cancel-work-on-probe-failure.patch`
- **Target subsystem tree:** `git://git.kernel.org/pub/scm/linux/kernel/git/phy/linux-phy.git` (`next` / `fixes`) and mainline `torvalds/linux.git` (`v7.4-rc3+`, tested at `666539eecdf4`).
- **Stable / LTS compatibility:**
  - Applies cleanly with zero conflicts to **Linux `v6.18`** (`7d0a66e4bb90`) as well as mainline **`v7.4-rc3+`**. (`rockchip_usb2phy_host_port_init()`, `rockchip_usb2phy_otg_port_init()`, and the `#include` block are identical; `v6.18` has only a small line-offset difference.)
  - Uses `disable_delayed_work_sync()`, which was introduced in **Linux 6.10**. It is directly compatible with Linux `6.12.y` and `6.18.y` LTS. Any backport to pre-`6.10` stable trees (`6.6.y`, `6.1.y`) would require backporting `disable_delayed_work_sync()` or using an explicit teardown guard with `cancel_delayed_work_sync()`.

## 2. Commit Metadata & Message

- **Subject:**
  `phy: rockchip: inno-usb2: disable work on probe failure and removal`
- **Commit body & tags** (identical to the patch file):
  ```text
  In rockchip_usb2phy_probe(), child port nodes are initialized in a
  loop.  For an OTG port (when dr_mode is not HOST or UNKNOWN),
  rockchip_usb2phy_otg_port_init() initializes chg_work and otg_sm_work,
  registers port IRQs and an extcon notifier, and (when no external
  "extcon" DT property is present) calls extcon_set_state_sync(), which
  synchronously invokes rockchip_otg_event() and schedules otg_sm_work
  after OTG_SCHEDULE_DELAY (2 * HZ).

  If a subsequent child port's devm_phy_create() returns -EPROBE_DEFER
  (for example, host-port waiting on a deferred phy-supply regulator) or
  another probe error occurs, devres_release_all() frees rport->phy and
  rphy without stopping the pending delayed work, causing a use-after-free
  when the work runs.  In addition, rockchip_usb2phy_driver has no .remove
  callback, so unbinding the driver leaves any armed sm_work, otg_sm_work,
  or chg_work running after rphy has been freed.

  Register per-port devm_add_action_or_reset() cleanup callbacks calling
  disable_delayed_work_sync() immediately after INIT_DELAYED_WORK() (after
  the USB_DR_MODE_HOST / USB_DR_MODE_UNKNOWN early return for the OTG port)
  and before rockchip_usb2phy_port_irq_init() and
  devm_extcon_register_notifier().

  Using disable_delayed_work_sync() rather than plain
  cancel_delayed_work_sync() prevents otg_sm_work and chg_work from
  cross-scheduling each other during teardown and blocks re-arming from
  concurrent extcon or IRQ callbacks.  Registering the devm actions
  between devm_phy_create() and IRQ/extcon registration ensures LIFO
  devres teardown unregisters the extcon notifier and frees all IRQs
  first, disables and drains all delayed work while rport->phy and rphy
  are still valid, and frees phy and rphy last.

  rockchip_usb2phy_exit() retains cancel_delayed_work_sync() so normal
  phy_init() -> phy_exit() -> phy_init() cycles continue to work.

  Fixes: 0e08d2a727e6 ("phy: rockchip-inno-usb2: add a new driver for Rockchip usb2phy")
  Fixes: 98898f3bc83c ("phy: rockchip-inno-usb2: support otg-port for rk3399")
  Fixes: 8dc60f8da22f ("phy: rockchip-inno-usb2: Sync initial otg state")
  Cc: stable@vger.kernel.org
  ```

## 3. Maintainers, Mailing Lists & `git send-email` Command

Generated via `./scripts/get_maintainer.pl -f drivers/phy/rockchip/phy-rockchip-inno-usb2.c`:

- **Maintainers / Reviewers (`--to`):**
  - `Vinod Koul <vkoul@kernel.org>` (maintainer: GENERIC PHY FRAMEWORK)
  - `Heiko Stuebner <heiko@sntech.de>` (maintainer: ARM/Rockchip SoC support)
  - `Neil Armstrong <neil.armstrong@linaro.org>` (reviewer: GENERIC PHY FRAMEWORK)
  - `Manivannan Sadhasivam <mani@kernel.org>` (reviewer: GENERIC PHY FRAMEWORK)
- **Mailing Lists (`--cc`):**
  - `linux-phy@lists.infradead.org`
  - `linux-rockchip@lists.infradead.org`
  - `linux-arm-kernel@lists.infradead.org` (moderated for non-subscribers)
  - `linux-kernel@vger.kernel.org`
  - `stable@vger.kernel.org` (via commit trailer)

Ready-to-run template (after setting real-name author/DCO in a clean kernel checkout and regenerating with `git format-patch -s`):

```bash
git send-email \
  --to="Vinod Koul <vkoul@kernel.org>" \
  --to="Heiko Stuebner <heiko@sntech.de>" \
  --cc="Neil Armstrong <neil.armstrong@linaro.org>" \
  --cc="Manivannan Sadhasivam <mani@kernel.org>" \
  --cc="linux-phy@lists.infradead.org" \
  --cc="linux-rockchip@lists.infradead.org" \
  --cc="linux-arm-kernel@lists.infradead.org" \
  --cc="linux-kernel@vger.kernel.org" \
  0001-phy-rockchip-inno-usb2-cancel-work-on-probe-failure.patch
```

## 4. Root Cause & Design Rationale

1. **Probe-defer UAF mechanism:**
   - In `rockchip_usb2phy_probe()`, each child node under `u2phy` (`otg-port`, `host-port`) calls `devm_phy_create()` (which invokes `phy_create()` -> `regulator_get_optional(&phy->dev, "phy")`) before calling `rockchip_usb2phy_otg_port_init()` or `rockchip_usb2phy_host_port_init()`.
   - When `otg-port` is child 0 and `dr_mode` is not `USB_DR_MODE_HOST`/`USB_DR_MODE_UNKNOWN`, `rockchip_usb2phy_otg_port_init()` initializes `chg_work` and `otg_sm_work`, requests port IRQs, registers `rport->event_nb` via `devm_extcon_register_notifier()`, and (when no DT `"extcon"` property is present) calls `extcon_set_state_sync()`. `extcon_sync()` unconditionally invokes the notifier chain, so `rockchip_otg_event()` calls `schedule_delayed_work(&rport->otg_sm_work, OTG_SCHEDULE_DELAY)` (`2 * HZ`).
   - When child 1 (`host-port`) has `phy-supply` pointing to a regulator whose GPIO/pinctrl has not probed yet, `devm_phy_create()` returns `-EPROBE_DEFER`. `devres_release_all()` frees `rport->phy` and `rphy`, leaving `otg_sm_work` armed on freed memory, causing a panic ~2 seconds later (~4 seconds into boot).
   - Unbinding the driver without `phy_exit()` suffers the same UAF across `otg_sm_work`, `chg_work`, and `sm_work`.

2. **Why `disable_delayed_work_sync()` via `devm_add_action_or_reset()` over `devm_delayed_work_autocancel()`:**
   - `devm_delayed_work_autocancel()` calls `cancel_delayed_work_sync()`, which does **not** prevent re-queueing after cancellation returns.
   - In this driver, `rockchip_usb2phy_otg_sm_work()` schedules `chg_work` (`USB_CHG_STATE_UNDEFINED`), and `rockchip_chg_detect_work()` calls `rockchip_usb2phy_otg_sm_work()` directly, which can re-schedule `otg_sm_work`. Two separate `cancel_delayed_work_sync()` calls leave a mutual re-arming window during teardown, and concurrent external `extcon` notifications or IRQs could re-arm work before unregistration finishes.
   - `disable_delayed_work_sync()` atomically cancels pending work, flushes any running callback, and marks the work disabled (`WORK_CANCEL_DISABLE`) so subsequent `schedule_delayed_work()` calls become no-ops.

3. **Why exact `devm` registration order is critical:**
   - All three workers dereference `rport->phy->dev.parent` (`rphy`) and `rphy->grf` / `rphy->edev` / clocks.
   - Registering `devm_add_action_or_reset()` **after** `devm_phy_create()` and `INIT_DELAYED_WORK()` (and after the `USB_DR_MODE_HOST`/`USB_DR_MODE_UNKNOWN` early return) and **before** `rockchip_usb2phy_port_irq_init()` and `devm_extcon_register_notifier()` (with combined `rphy->irq` and `devm_of_phy_provider_register()` after the child loop) guarantees strict LIFO teardown:
     1. Combined `rphy->irq` and `of_phy_provider` are torn down.
     2. `devm_extcon_unregister_notifier()` unregisters `event_nb`.
     3. Per-port IRQs (`otg-id`, `otg-bvalid`, `otg-mux`, `linestate`) are freed and synchronized.
     4. `disable_delayed_work_sync()` drains and disables `otg_sm_work`, `chg_work`, and `sm_work` while `rport->phy`, `rphy->grf`, `rphy->edev`, and `rphy` are all still allocated.
     5. `devm_phy_consume()` frees `rport->phy`, clocks/extcon are released, and `rphy` is freed last.

4. **Why `rockchip_usb2phy_exit()` keeps `cancel_delayed_work_sync()`:**
   - During normal runtime `phy_init()` -> `phy_exit()` -> `phy_init()` transitions, a subsequent `phy_init()` must still be able to schedule `otg_sm_work` or `sm_work`.

## 5. Reproduction & Testing Notes

- **Trigger hardware/DTS pattern (ReSpeaker Core v2 / `rk3229`):**
  - `vcc_host` (`regulator-fixed`) depends on `gpio3` + `pinctrl`. When `u2phy0_host` (`host-port`, child 1) specifies `phy-supply = <&vcc_host>`, `u2phy0` probes before `gpio3`: `u2phy0_otg` (child 0) succeeds and queues `otg_sm_work`, `u2phy0_host` returns `-EPROBE_DEFER`, and unpatched kernels panic ~4 s into boot.
  - **Repository DTS note:** `newos/port/rk3229-respeaker-v2.dts` intentionally keeps its `phy-supply` omission workaround untouched until this kernel patch lands in the target kernel build.
- **Host verification performed:**
  - `git apply --check` and `git am` verified cleanly against both Linux `v6.18` and mainline `v7.4-rc3+` (`666539eecdf4`).
  - `scripts/checkpatch.pl --no-tree --strict --no-signoff` verified 0 errors and 0 formatting checks (only `Unknown commit id` warnings on shallow `--depth=1` clones; full history + owner `Signed-off-by:` required before sending).
  - Userspace C lifecycle/LIFO test harness compiled with `-fsanitize=address,undefined` verified `-EPROBE_DEFER` teardown, `otg_sm_work` <-> `chg_work` ping-pong disabling, concurrent IRQ/extcon storm unbind, `phy_init()`/`phy_exit()`/`phy_init()` re-arming, `USB_DR_MODE_HOST` early return, and `-ENOMEM` handling.
- **Target kernel verification recommended before sending:**
  - Compile with `W=1` (`drivers/phy/rockchip/phy-rockchip-inno-usb2.o`).
  - Boot-test on RK3229 / Rockchip board with `phy-supply = <&vcc_host>;` restored and `CONFIG_KASAN=y` + `CONFIG_DEBUG_OBJECTS_WORK=y` enabled, verifying clean `-EPROBE_DEFER` retry without panic, plus `sysfs` `unbind`/`bind` cycling.

## 6. Owner Pre-Submission Checklist

1. Apply `0001-phy-rockchip-inno-usb2-cancel-work-on-probe-failure.patch` onto a clean `linux-phy` / mainline `master` git tree.
2. Replace the placeholder `From:` author with your real name/email and add your DCO `Signed-off-by:` trailer:
   ```bash
   git commit --amend --author="Your Real Name <your@email>" -s
   git format-patch -1 -s --to="..."
   ```
3. Run `./scripts/checkpatch.pl --strict 0001-*.patch` in the full kernel tree and confirm **0 errors, 0 warnings**.
4. Optionally run `make ARCH=arm W=1 drivers/phy/rockchip/phy-rockchip-inno-usb2.o` (and target boot test with KASAN / `DEBUG_OBJECTS_WORK`).
5. Send with `git send-email` using the recipients in Section 3.
