package rw.agent;

import java.util.ArrayList;
import java.util.Iterator;

import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.y;
import com.corrodinggames.rts.gameFramework.l;

/**
 * 建造任务全生命周期追踪（诊断用）：记录 builder 的路点何时出现、何时消失、
 * 目标点附近是否真的出现了施工中的建筑（cm<1）。数据只进日志。
 */
public final class BuildWatch {
    private static final int MAX_AGE_TICKS = 2400; // 40 秒

    private static final class Watch {
        long builderId;
        float x;
        float y;
        int startTick;
        int lastLogTick;
        boolean hadWaypoint;
        String lastAct = "";
    }

    private static final ArrayList<Watch> watches = new ArrayList<Watch>();

    private BuildWatch() {
    }

    public static void add(am builder, float x, float y) {
        try {
            Watch w = new Watch();
            w.builderId = builder.eh;
            w.x = x;
            w.y = y;
            w.startTick = l.B().bx;
            w.lastLogTick = w.startTick;
            watches.add(w);
            Log.info("BW+ #" + builder.eh + " -> (" + (int) x + "," + (int) y + ") 开始追踪");
        } catch (Throwable ignored) {
        }
    }

    /** Game thread only (TickLoop 每 tick 调). */
    public static void tick() {
        if (watches.isEmpty()) {
            return;
        }
        l engine = l.B();
        int bx = engine.bx;
        Iterator<Watch> it = watches.iterator();
        while (it.hasNext()) {
            Watch w = it.next();
            try {
                am builder = com.corrodinggames.rts.gameFramework.w.a(w.builderId, true);
                if (builder == null || builder.bV || builder.ej) {
                    Log.info("BW! #" + w.builderId + " 建造者死亡/移除");
                    it.remove();
                    continue;
                }
                String act = Activity.describe(builder);
                if (act == null) {
                    act = "?";
                }
                boolean hasWp = !"idle".equals(act) && !"?".equals(act);
                if (hasWp) {
                    w.hadWaypoint = true;
                }
                boolean constructionNearby = constructionNear(engine, w.x, w.y);
                if (constructionNearby) {
                    Log.info("BW✓ #" + w.builderId + " 目标点出现施工建筑 act=" + act);
                    it.remove();
                    continue;
                }
                if (w.hadWaypoint && !hasWp) {
                    Log.info("BW✗ #" + w.builderId + " 路点消失于 t+" + (bx - w.startTick) / 60.0f
                        + "s，目标点无施工建筑！act=" + act);
                    it.remove();
                    continue;
                }
                if (bx - w.lastLogTick >= 600) {
                    w.lastLogTick = bx;
                    Log.info("BW~ #" + w.builderId + " act=" + act + " pos=(" + (int) builder.eo + ","
                        + (int) builder.ep + ") 目标=(" + (int) w.x + "," + (int) w.y + ")");
                }
                if (bx - w.startTick > MAX_AGE_TICKS) {
                    Log.info("BW? #" + w.builderId + " 超时(40s)仍未建成 act=" + act);
                    it.remove();
                }
            } catch (Throwable t) {
                Log.error("BW tick error", t);
                it.remove();
            }
        }
    }

    private static boolean constructionNear(l engine, float x, float y) {
        com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
        Object[] arr = units.b();
        int size = units.size();
        for (int i = 0; i < size; i++) {
            Object obj = arr[i];
            if (!(obj instanceof am)) {
                continue;
            }
            am u = (am) obj;
            if (u.bV || u.ej || u.cm >= 1.0f) {
                continue;
            }
            if (Math.hypot(u.eo - x, u.ep - y) < 80.0) {
                return true;
            }
        }
        return false;
    }
}
