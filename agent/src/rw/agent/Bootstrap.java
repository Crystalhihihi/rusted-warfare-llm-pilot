package rw.agent;

import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.game.units.ar;

/**
 * Waits for the engine + a loaded map, then registers a BridgeObject into the
 * game's global object list. Re-registers when a new match starts (tick counter
 * regression) or the object was removed. Runs on a daemon thread; all real work
 * happens inside the game thread via the tick callback.
 */
public final class Bootstrap implements Runnable {
    private static OverlayObject overlay;
    static volatile String dumpPediaPath = null; // agent 参数 dumpPedia=<path> 启用
    private static boolean pediaDumped = false;
    private static int pediaStableSize = -1;
    private static int pediaFailures = 0;

    @Override
    public void run() {
        while (true) {
            try {
                tick();
            } catch (Throwable t) {
                Log.error("bootstrap loop error", t);
                sleep(5000);
                continue;
            }
            sleep(1000);
        }
    }

    private static void tick() {
        l engine = l.B();
        if (dumpPediaPath != null && !pediaDumped) {
            tryDumpPedia();
        }
        if (engine == null || engine.bL == null) {
            return; // no engine yet, or sitting in menu
        }
        if (!stale(engine)) {
            return;
        }
        // Frame updates hold engine.aj for the whole frame, so registering under
        // this lock keeps us mutually exclusive with the game thread.
        synchronized (engine.aj) {
            if (!stale(engine)) {
                return;
            }
            BridgeObject.discardCurrent();
            discardOverlay();
            BridgeObject bo = new BridgeObject(); // constructor self-registers into w.er
            overlay = new OverlayObject();
            BridgeObject.callbackTick = engine.bx;
            Log.info("BridgeObject+OverlayObject registered at tick=" + engine.bx + " obj=" + bo);
        }
    }

    /** 诊断 dump：等单位注册表大小稳定（连续两拍不变且非空）后导出 Pedia，最多重试 10 次。 */
    private static void tryDumpPedia() {
        try {
            if (ar.ae == null) {
                return;
            }
            int size = ar.ae.size();
            if (size == 0 || size != pediaStableSize) {
                pediaStableSize = size;
                return;
            }
            String p = Pedia.build();
            if (p == null) {
                return;
            }
            java.nio.file.Files.write(java.nio.file.Paths.get(dumpPediaPath), p.getBytes("UTF-8"));
            pediaDumped = true;
            Log.info("pedia dumped: " + dumpPediaPath + " units=" + size);
        } catch (Throwable t) {
            Log.error("dumpPedia error", t);
            if (++pediaFailures >= 10) {
                pediaDumped = true; // 放弃，不再刷屏
            }
        }
    }

    private static void discardOverlay() {
        if (overlay != null) {
            try {
                overlay.a();
            } catch (Throwable ignored) {
            }
            overlay = null;
        }
    }

    /**
     * We can silently fall out of the object list when a match re-initializes
     * (list cleared without ej being set). Detect it: the engine tick advances
     * but our callback stops being invoked. Pause freezes bx, so it can't cause
     * a false positive.
     */
    private static boolean stale(l engine) {
        if (!BridgeObject.alive()) {
            return true;
        }
        int cb = BridgeObject.callbackTick;
        return engine.bx < cb || engine.bx - cb > 120;
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}
