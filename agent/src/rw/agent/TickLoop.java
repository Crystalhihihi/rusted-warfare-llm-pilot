package rw.agent;

import com.corrodinggames.rts.game.n;
import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.gameFramework.l;

import java.net.Socket;

/**
 * Per-tick driver, invoked from BridgeObject on the game thread:
 *  - drain inbound bridge commands (logged for now; executed in M2)
 *  - emit a state snapshot to the bridge every SNAP_EVERY ticks
 *  - heartbeat log every LOG_EVERY ticks
 */
public final class TickLoop {
    private static final int SNAP_EVERY = 30;   // ~0.5s at 60 tps
    private static final int LOG_EVERY = 1500;  // ~25s
    private static int lastLog = -99999;
    private static Object lastMap = null;
    private static boolean mapSent = false;
    private static boolean prevHasClient = false;

    private TickLoop() {
    }

    public static void onTick(float dt) {
        l engine = l.B();
        int bx = engine.bx;

        if (engine.bs != null && !engine.bs.w) {
            Commands.trackSelection(engine.bs);
        }
        ChatCommands.tick();
        OverlayData.clickPoll();
        BuildWatch.tick();

        String cmd;
        while ((cmd = NetServer.pollCommand()) != null) {
            Commands.handle(cmd);
        }

        if (bx % SNAP_EVERY == 0 && NetServer.hasClient()) {
            try {
                NetServer.send(Snapshot.build());
            } catch (Throwable t) {
                Log.error("snapshot error", t);
            }
        }

        // per-match map data: send once when a map is loaded and a bridge is
        // connected; resend if the bridge reconnects mid-match
        boolean hasClient = NetServer.hasClient();
        if (engine.bL != lastMap) {
            lastMap = engine.bL;
            mapSent = false;
            ChatPanel.clear();  // 战役连打：上一局指令流不残留（"上一把的任务还在"的根治）
            ChatPanel.add("system", "—— 新对局（地图已切换，上一局指令流已清空）——");
        }
        if (hasClient && !prevHasClient) {
            mapSent = false;
        }
        prevHasClient = hasClient;
        if (hasClient && !mapSent && engine.bL != null) {
            try {
                String m = MapData.build();
                if (m != null) {
                    NetServer.send(m);
                    Log.info("map data sent");
                }
                String p = Pedia.build();
                if (p != null) {
                    NetServer.send(p);
                    Log.info("pedia sent");
                }
                AiState.notifyChange(); // 同步 AI 控制状态给新连接的 bridge
                mapSent = true;
            } catch (Throwable t) {
                Log.error("mapdata error", t);
                mapSent = true; // don't retry-spam every tick
            }
        }
        // 新客户端补发（暂停期换桥接：client edge 被 prevHasClient 陈化错过，welcome 队列无死角）
        Socket w = NetServer.pollWelcome();
        if (w != null && engine.bL != null) {
            try {
                String wm = MapData.build();
                if (wm != null) {
                    NetServer.sendTo(w, wm);
                }
                String wp = Pedia.build();
                if (wp != null) {
                    NetServer.sendTo(w, wp);
                }
                AiState.notifyChange();
                Log.info("welcome resend done (map+pedia)");
            } catch (Throwable t) {
                Log.error("welcome resend error", t);
            }
        }

        if (bx < lastLog) {
            lastLog = bx - LOG_EVERY; // new match: tick counter restarted
        }
        if (bx - lastLog >= LOG_EVERY) {
            lastLog = bx;
            n team = engine.bs;
            String teamInfo = team == null ? "none" : ("id=" + team.k + " funds=" + (long) team.o + " ai=" + team.w);
            Log.info("tick=" + bx + " units=" + am.bF().size() + " localTeam={" + teamInfo + "}"
                + " bridge=" + NetServer.hasClient());
        }
    }
}
