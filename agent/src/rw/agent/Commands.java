package rw.agent;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import android.graphics.PointF;

import com.corrodinggames.rts.game.n;
import com.corrodinggames.rts.game.units.ak;
import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.ar;
import com.corrodinggames.rts.game.units.as;
import com.corrodinggames.rts.game.units.ao;
import com.corrodinggames.rts.game.units.y;
import com.corrodinggames.rts.gameFramework.e;
import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.w;

/**
 * Executes bridge commands on the game thread, through the game's own command
 * pipeline (l.cf) — same path as mouse clicks, so lockstep sync is preserved.
 *
 * Delegation model: control-group slots in delegatedSlots (default {8} = 数字键9)
 * are AI-managed. The bridge can change the set at runtime via the "delegate"
 * command. Commands only ever target units in those slots, and only ours.
 *
 * Protocol (NDJSON in):
 *   {"cmd":"move","x":..,"y":..}  {"cmd":"attackmove","x","y"}  {"cmd":"stop"}
 *   {"cmd":"attack","id":..}
 *   {"cmd":"delegate","groups":"0,3,9"}
 *   {"cmd":"build","action":"s_extractorT1","x":..,"y":..}
 *   {"cmd":"train","action":"s_tank","factory":unitId}
 *   {"cmd":"rally","x":..,"y":..,"factory":unitId}
 *   {"cmd":"load","target":运输舰id,"units":"被装单位id,逗号分隔"}
 *   {"cmd":"unload","target":运输舰id,"x":..,"y":..}
 *   {"cmd":"launch","id":核弹井id,"x":..,"y":..}
 *   {"cmd":"overlay","text":"line1\\nline2"}   (handled by OverlayData)
 *   {"cmd":"speed","x":8}                     游戏速度 0.1-64x（单人/沙盒上限）
 * Every handled command gets a {"type":"cmdResult",...} reply.
 */
public final class Commands {
    private static volatile Set<Integer> delegatedSlots = Collections.emptySet();

    private static boolean reflectInit = false;
    private static Field hudControlGroups;   // f.g.g  (f.a)
    private static Field groupsList;         // f.a.aA (ArrayList<f.am>)
    private static Field groupMembers;       // f.am.a (ArrayList<am>)

    private Commands() {
    }

    public static void handle(String line) {
        String cmd = strVal(line, "cmd");
        if (cmd == null) {
            reply(false, "?", "unparsable cmd line");
            return;
        }
        if ("overlay".equals(cmd)) {
            OverlayData.handle(line);
            return;
        }
        l engine = l.B();
        if ("speed".equals(cmd)) {
            // 游戏速度（单人/沙盒上限 64x，gameFramework/l.bt）；沙盒探针用
            Float v = numVal(line, "x");
            if (v == null) {
                reply(false, cmd, "missing x");
                return;
            }
            float f = Math.max(0.1f, Math.min(64f, v.floatValue()));
            try {
                java.lang.reflect.Field bt = l.class.getField("bt");
                bt.setFloat(engine, f);
                reply(true, cmd, "speed=" + f);
            } catch (Throwable ex) {
                reply(false, cmd, "speed set failed: " + ex.getMessage());
            }
            return;
        }
        n team = engine.bs;
        if (team == null) {
            reply(false, cmd, "no local team");
            return;
        }
        try {
            if ("move".equals(cmd) || "attackmove".equals(cmd)) {
                Float x = numVal(line, "x");
                Float yv = numVal(line, "y");
                if (x == null || yv == null) {
                    reply(false, cmd, "missing x/y");
                    return;
                }
                // 逐单位过滤：不可达的排除；整队指令自动跳过施工中的单位和建筑（除非 ids 显式点名）
                ArrayList<am> targets = filterByIds(delegatedUnits(team), line);
                ArrayList<am> reach = new ArrayList<am>();
                int excluded = 0;
                int busy = 0;
                int bldSkip = 0;
                boolean hasIds = strVal(line, "units") != null;
                for (am u : targets) {
                    if (!hasIds) {
                        if (isBuilding(u)) {
                            bldSkip++;
                            continue;
                        }
                        // 战斗指令不拖建造者上火线（空闲的也算，除非 ids 显式点名）
                        try {
                            if (u.r().l()) {
                                busy++;
                                continue;
                            }
                        } catch (Throwable ignored) {
                        }
                        String act = Activity.describe(u);
                        if (act != null && (act.startsWith("build") || act.startsWith("repair"))) {
                            busy++; // 施工走路(build@)与盖着(repair→)都算施工中
                            continue;
                        }
                    }
                    if (canReach(u, x.floatValue(), yv.floatValue())) {
                        reach.add(u);
                    } else {
                        excluded++;
                    }
                }
                if (reach.isEmpty() && !targets.isEmpty()) {
                    reply(false, cmd, "目标点对全部受令单位不可达（被水/岩浆/高崖隔断）。"
                        + findDetour(targets, x.floatValue(), yv.floatValue()));
                    return;
                }
                e c = engine.cf.b(team);
                int n0 = 0;
                for (am u : reach) {
                    if (u instanceof y) {
                        c.a((y) u);
                        n0++;
                    }
                }
                if (n0 == 0) {
                    reply(false, cmd, "no delegated units");
                    return;
                }
                if ("move".equals(cmd)) {
                    c.a(x.floatValue(), yv.floatValue());
                } else {
                    c.b(x.floatValue(), yv.floatValue());
                }
                reply(true, cmd, n0 + " units @(" + x.intValue() + "," + yv.intValue() + ")"
                    + (excluded > 0 ? "（" + excluded + "个无法到达已排除）" : "")
                    + (busy > 0 ? "（" + busy + "个施工中已跳过）" : "")
                    + (bldSkip > 0 ? "（" + bldSkip + "个建筑已跳过）" : ""));
            } else if ("stop".equals(cmd)) {
                e c = engine.cf.b(team);
                int n0 = addDelegated(c, team, line);
                if (n0 == 0) {
                    reply(false, cmd, "no delegated units");
                    return;
                }
                c.h();
                reply(true, cmd, n0 + " units stopped");
            } else if ("attack".equals(cmd)) {
                Float id = numVal(line, "id");
                if (id == null) {
                    reply(false, cmd, "missing id");
                    return;
                }
                am target = w.a(id.longValue(), false);
                if (target == null || target.bV) {
                    reply(false, cmd, "target not found/dead");
                    return;
                }
                e c = engine.cf.b(team);
                int n0 = addDelegated(c, team, line);
                if (n0 == 0) {
                    reply(false, cmd, "no delegated units");
                    return;
                }
                c.a(target);
                reply(true, cmd, n0 + " units attack " + target.r().i());
            } else if ("resume".equals(cmd)) {
                // 续建未完工建筑：repair 路点（av.d），与玩家右键未完工建筑同一条路径
                Float bid = numVal(line, "builder");
                Float tid = numVal(line, "target");
                if (bid == null || tid == null) {
                    reply(false, cmd, "missing builder/target");
                    return;
                }
                am b = w.a(bid.longValue(), false);
                am t = w.a(tid.longValue(), false);
                if (b == null || b.bV || b.bX != team) {
                    reply(false, cmd, "builder not found/dead");
                    return;
                }
                if (t == null || t.bV || t.bX != team) {
                    reply(false, cmd, "target not found/dead");
                    return;
                }
                if (t.cm >= 1.0f && t.cu >= t.cv) {
                    reply(false, cmd, "target 完好，无需续建/修理");
                    return;
                }
                boolean isBuilder = false;
                try {
                    isBuilder = b.r().l();
                } catch (Throwable ignored) {
                }
                if (!isBuilder) {
                    reply(false, cmd, "指定的 builder 不是建造者");
                    return;
                }
                e c = engine.cf.b(team);
                c.a((y) b);
                c.b(t); // repair 路点：建造者会自己走进施工距离续建，完工/目标死亡游戏自动清路点
                reply(true, cmd, "builder#" + b.eh + " resume " + t.r().i() + "#" + t.eh);
            } else if ("buildq".equals(cmd)) {
                // 队列建造（UI shift 拉条同款机制）：第 1 条起全部 e=true 追加进 y.g 路点数组，
                // 游戏 av.c→av.d 改写 + 完工 ax() 自动推进，建造者连干 N 个建筑
                Float bid = numVal(line, "builder");
                String items = strVal(line, "items"); // "b_x@x,y;b_y@x,y;..."
                if (bid == null || items == null) {
                    reply(false, cmd, "missing builder/items");
                    return;
                }
                am b = w.a(bid.longValue(), false);
                if (b == null || b.bV || b.bX != team) {
                    reply(false, cmd, "builder not found/dead");
                    return;
                }
                boolean isBuilder = false;
                try {
                    isBuilder = b.r().l();
                } catch (Throwable ignored) {
                }
                if (!isBuilder) {
                    reply(false, cmd, "指定的 builder 不是建造者");
                    return;
                }
                if (!ensureActionReflect()) {
                    reply(false, cmd, "action reflection unavailable");
                    return;
                }
                int queued = 0;
                StringBuilder skipped = new StringBuilder();
                for (String it : items.split(";")) {
                    String[] pa = it.split("@");
                    if (pa.length != 2) {
                        continue;
                    }
                    String[] xy = pa[1].split(",");
                    if (xy.length != 2) {
                        continue;
                    }
                    as type = resolveType(pa[0]);
                    if (type == null) {
                        skipped.append(pa[0]).append('?').append(' ');
                        continue;
                    }
                    float px;
                    float py;
                    try {
                        px = Float.parseFloat(xy[0]);
                        py = Float.parseFloat(xy[1]);
                    } catch (NumberFormatException nfe) {
                        continue;
                    }
                    if (type.p()) {
                        // 池点类：游戏对非池心坐标静默丢路点（回执 ok 但不建）——
                        // 吸附最近池的 9 宫格合法点，与单条 build 同套自纠
                        float[] snap = snapToPool(engine, team, type, px, py);
                        if (snap == null) {
                            skipped.append(pa[0]).append("@(").append((int) px).append(",").append((int) py)
                                   .append("):附近无空闲池点 ");
                            continue;
                        }
                        px = snap[0];
                        py = snap[1];
                    } else {
                        String err = null;
                        try {
                            err = placementError(type, px, py, team);
                        } catch (Throwable ignored) {
                        }
                        if (err != null) {
                            skipped.append(pa[0]).append("@(").append((int) px).append(",").append((int) py)
                                   .append("):").append(placementReason(err)).append(' ');
                            continue; // 校验不过跳过该点，不影响队列其它路点
                        }
                    }
                    e c = engine.cf.b(team);
                    c.e = true; // 追加模式：不清队列，路点进队尾（e.java:13）
                    c.a((y) b);
                    c.a(px, py, type, 1);
                    queued++;
                }
                reply(true, cmd, "builder#" + b.eh + " buildq x" + queued
                    + (skipped.length() > 0 ? "（跳过: " + skipped + "）" : ""));
            } else if ("delegate".equals(cmd)) {
                String groups = strVal(line, "groups");
                if (groups == null) {
                    reply(false, cmd, "missing groups");
                    return;
                }
                HashSet<Integer> slots = new HashSet<Integer>();
                for (String s : groups.split(",")) {
                    s = s.trim();
                    if (s.isEmpty()) {
                        continue;
                    }
                    int slot = Integer.parseInt(s);
                    if (slot < 0 || slot > 9) {
                        reply(false, cmd, "slot out of range: " + slot);
                        return;
                    }
                    slots.add(slot);
                }
                delegatedSlots = Collections.unmodifiableSet(slots);
                Log.info("delegated slots -> " + slots);
                reply(true, cmd, "delegated=" + slots);
            } else if ("build".equals(cmd)) {                handleBuild(engine, team, line, cmd);
            } else if ("train".equals(cmd)) {
                handleTrain(engine, team, line, cmd);
            } else if ("rally".equals(cmd)) {
                handleRally(engine, team, line, cmd);
            } else if ("load".equals(cmd)) {
                handleLoad(engine, team, line, cmd);
            } else if ("unload".equals(cmd)) {
                handleUnload(engine, team, line, cmd);
            } else if ("launch".equals(cmd)) {
                handleLaunch(engine, team, line, cmd);
            } else {
                reply(false, cmd, "unknown cmd");
            }
        } catch (Throwable t) {
            Log.error("cmd '" + cmd + "' failed", t);
            reply(false, cmd, "exception: " + t);
        }
    }

    // ---- action-id plumbing (units.a.c is unreachable at compile time:
    // class units.a and package units.a/ collide, so we go via reflection) ----

    private static Class<?> actionIdClass;      // com.corrodinggames.rts.game.units.a.c
    private static java.lang.reflect.Method actionIdOf;   // static a(String) -> a.c
    private static java.lang.reflect.Method eSetAction;   // e.a(a.c)
    private static java.lang.reflect.Method eSetActionAt; // e.a(a.c, PointF, am)

    private static synchronized boolean ensureActionReflect() {
        if (actionIdClass != null) {
            return true;
        }
        try {
            actionIdClass = Class.forName("com.corrodinggames.rts.game.units.a.c");
            actionIdOf = actionIdClass.getMethod("a", String.class);
            actionIdOf.setAccessible(true); // 该类可能包私有（反射加载、编译期不可见）
            eSetAction = e.class.getMethod("a", actionIdClass);
            eSetActionAt = e.class.getMethod("a", actionIdClass, PointF.class, am.class);
            Log.info("action-id reflection ok");
            return true;
        } catch (Throwable t) {
            Log.error("action-id reflection failed", t);
            return false;
        }
    }

    private static void setAction(Object cmdObj, String action, Float x, Float y) throws Exception {
        Object actId = actionIdOf.invoke(null, action);
        if (x != null && y != null) {
            eSetActionAt.invoke(cmdObj, actId, new PointF(x.floatValue(), y.floatValue()), null);
        } else {
            eSetAction.invoke(cmdObj, actId);
        }
    }

    /** 反射调用目标可能属于包私有游戏类（如 units.d.u）的 public 方法：
     *  Java 规则下 invoke 前必须 setAccessible(true)，否则 IllegalAccessException。
     *  对已是 public 的类，setAccessible 是 no-op。 */
    private static Object invokePub(Object target, String name, Class<?>[] sig, Object... args) throws Exception {
        java.lang.reflect.Method m = target.getClass().getMethod(name, sig);
        m.setAccessible(true);
        return m.invoke(target, args);
    }

    // ---- build / train / rally ----

    private static void handleBuild(l engine, n team, String line, String cmd) throws Exception {
        if (!ensureActionReflect()) {
            reply(false, cmd, "action reflection unavailable");
            return;
        }
        String action = strVal(line, "action");
        Float x = numVal(line, "x");
        Float yv = numVal(line, "y");
        if (action == null) {
            reply(false, cmd, "missing action");
            return;
        }
        as type = resolveType(action);
        if (type == null) {
            reply(false, cmd, "unknown unit type for action " + action);
            return;
        }
        try {
            if (team.o < type.c()) {
                reply(false, cmd, "funds " + (long) team.o + " < price " + type.c());
                return;
            }
        } catch (Throwable ignored) {
        }
        am builder = null;
        Float wantId = numVal(line, "builder");
        if (wantId != null) {
            // LLM 指定了建造者 id：校验是我方建造者
            am want = w.a(wantId.longValue(), false);
            if (want != null && !want.bV && want.bX == team) {
                try {
                    if (want.r().l()) {
                        builder = want;
                    }
                } catch (Throwable ignored) {
                }
            }
            if (builder == null) {
                reply(false, cmd, "指定的 builder id 不是可用建造者");
                return;
            }
        } else {
            // 默认选择：优先没有施工任务的建造者（act 非 build 开头），都没有再用第一个
            am fallback = null;
            for (am u : delegatedUnits(team)) {
                try {
                    if (!u.r().l()) {
                        continue;
                    }
                    if (fallback == null) {
                        fallback = u;
                    }
                    String act = Activity.describe(u);
                    if (act == null || !act.startsWith("build")) {
                        builder = u;
                        break;
                    }
                } catch (Throwable ignored) {
                }
            }
            if (builder == null) {
                builder = fallback;
            }
        }
        if (builder == null) {
            reply(false, cmd, "no builder in delegated units");
            return;
        }
        // 施工保护：目标建造者已有施工任务时拒绝（防顶掉原施工；build@与repair→都算）
        try {
            String bAct = Activity.describe(builder);
            if (bAct != null && (bAct.startsWith("build") || bAct.startsWith("repair"))) {
                reply(false, cmd, "builder#" + builder.eh + " 正在施工(" + bAct + ")，完工后再派新任务（或换其他空闲建造者）");
                return;
            }
        } catch (Throwable ignored) {
        }
        // —— Easy Build 自动选址：LLM 给类型(+可选锚点)，agent 找合法位置（游戏权威校验把关）——
        float ax;
        float ay;
        if (x != null && yv != null) {
            ax = x.floatValue();
            ay = yv.floatValue();
        } else {
            am cc = findCommandCenter(team);
            if (cc == null) {
                reply(false, cmd, "找不到主基地做选址锚点");
                return;
            }
            ax = cc.eo;
            ay = cc.ep;
        }
        StringBuilder spotReason = new StringBuilder();
        float[] spot = findBuildSpot(engine, team, type, ax, ay, spotReason, builder);
        if (spot == null) {
            String extra = "";
            try {
                if (type.p()) {
                    String fp = freePoolsText(engine, ax, ay, 5);
                    if (!fp.isEmpty()) {
                        extra = "；空闲池(近→远): " + fp;
                    }
                }
            } catch (Throwable ignored) {
            }
            reply(false, cmd, "@(" + (int) ax + "," + (int) ay + ") 找不到合法建造位置：" + spotReason + extra);
            return;
        }
        x = spot[0];
        yv = spot[1];
        // 可达性预检：目标 85px 环带采样——建造者能走到施工距离内任一点即放行（岸边造水上建筑合法）
        if (!builderCanApproach(builder, x.floatValue(), yv.floatValue())) {
            reply(false, cmd, "@(" + (int) x.floatValue() + "," + (int) yv.floatValue() + ") 建造者无法靠近目标85px内（水域/隔断），需换海上建造者或换位置");
            return;
        }
        // 前缀归一：建造指令只认 b_（s_ 是"选中同类"动作，存在于每个单位，命中即空操作）
        if (action.startsWith("s_")) {
            action = "b_" + action.substring(2);
        }
        // 预检：解析实际动作 id（s_/b_/u_ 回退），并校验当前可用性
        String resolvedB = resolveActionOn(builder, action);
        if (resolvedB == null) {
            String avail = availableActions(builder, 10);
            reply(false, cmd, "建造者没有此动作: " + action + (avail.isEmpty() ? "" : "；可用: " + avail));
            dumpActions(builder, "build " + action);
            return;
        }
        action = resolvedB;
        Object actOnB = lookupAction(builder, action);
        Boolean availB = (Boolean) invokePub(actOnB, "b", new Class<?>[]{am.class}, builder);
        if (availB == null || !availB) {
            reply(false, cmd, "动作当前不可用（科技/条件不满足）: " + action);
            return;
        }
        e c = engine.cf.b(team);
        c.a((y) builder);
        // 施工走路径点通道（av.c 建造路点），与 UI 落点同一条路径
        c.a(x.floatValue(), yv.floatValue(), type, 1);
        BuildWatch.add(builder, x.floatValue(), yv.floatValue());
        reply(true, cmd, "builder#" + builder.eh + " build " + action + " @(" + x.intValue() + "," + yv.intValue() + ")");
    }

    private static void handleTrain(l engine, n team, String line, String cmd) throws Exception {
        if (!ensureActionReflect()) {
            reply(false, cmd, "action reflection unavailable");
            return;
        }
        String action = strVal(line, "action");
        Float fid = numVal(line, "factory");
        if (action == null || fid == null) {
            reply(false, cmd, "missing action/factory");
            return;
        }
        am fac = w.a(fid.longValue(), false);
        if (fac == null || fac.bV || fac.bX != team) {
            reply(false, cmd, "factory not found/not ours");
            return;
        }
        if (!(fac instanceof y)) {
            reply(false, cmd, "factory not commandable");
            return;
        }
        if (fac.cm < 1.0f) {
            reply(false, cmd, "工厂尚未完工（" + (int) (fac.cm * 100) + "%），等完工再生产");
            return;
        }
        // 前缀归一：生产指令只认 u_ 或数字升级（s_ 是"选中同类"动作，命中即空操作）
        if (action.startsWith("s_")) {
            action = "u_" + action.substring(2);
        }
        // 资金预检：生产在入队时全额扣款，不够则静默失败
        as ttype = resolveType(action);
        if (ttype != null) {
            try {
                if (team.o < ttype.c()) {
                    reply(false, cmd, "funds " + (long) team.o + " < price " + ttype.c());
                    return;
                }
            } catch (Throwable ignored) {
            }
        }
        // 预检：动作必须在该单位的可用动作列表里且当前可用（游戏对失败是静默的）
        String resolvedT = resolveActionOn(fac, action);
        if (resolvedT == null) {
            String avail = availableActions(fac, 12);
            reply(false, cmd, "工厂#" + fac.eh + " 没有此动作: " + action + (avail.isEmpty() ? "" : "；可用: " + avail));
            dumpActions(fac, "train " + action);
            return;
        }
        action = resolvedT;
        Object actOnUnit = lookupAction(fac, action);
        Boolean avail = (Boolean) invokePub(actOnUnit, "b", new Class<?>[]{am.class}, fac);
        if (avail == null || !avail) {
            reply(false, cmd, "动作当前不可用（科技/条件不满足）: " + action);
            return;
        }
        e c = engine.cf.b(team);
        c.a((y) fac);
        setAction(c, action, null, null);
        reply(true, cmd, fac.r().i() + " train " + action);
    }

    private static void handleRally(l engine, n team, String line, String cmd) {
        Float x = numVal(line, "x");
        Float yv = numVal(line, "y");
        Float fid = numVal(line, "factory");
        if (x == null || yv == null || fid == null) {
            reply(false, cmd, "missing x/y/factory");
            return;
        }
        am fac = w.a(fid.longValue(), false);
        if (fac == null || fac.bV || fac.bX != team || !(fac instanceof y)) {
            reply(false, cmd, "factory not found/not ours");
            return;
        }
        e c = engine.cf.b(team);
        c.a((y) fac);
        c.a(new PointF(x.floatValue(), yv.floatValue()));
        reply(true, cmd, "rally @(" + x.intValue() + "," + yv.intValue() + ")");
    }

    // ---- load / unload / launch（运输与核发射，全部走 engine.cf 原生命令队列）----
    //
    // 引擎路径（反编译 decompiled/game-lib 核实）：
    // 装载：玩家右键运输舰 = 选中被装单位 + 命令.e(运输舰) → av.n 路点
    //   （gameFramework/f/g.java:3387 f()；AI 同款 game/a/n.java:239）。
    //   被装单位自行走到运输舰旁并进入；能否装载由运输舰 d(单位,false) 判定
    //   （units/e/i.java:299 hovercraft / units/b/d.java:344 dropship /
    //   units/custom/j.java:287 experimentalDropship）。
    // 卸货：运输舰的"unload"动作（ActionId("109")，units/e/i.java:22 i$1(109)）。
    //   命令.a(动作) 特殊动作分发（gameFramework/e.java:621 k()）→ 运输舰开始卸货，
    //   每 30 tick 放下一单位（units/e/i.java:215）。ini 系运输舰的动作表也注册了
    //   同一动作（units/custom/l.java:707），cp() 对三类运输舰都返回它
    //   （units/custom/j.java:3915）。
    // 核发射：nukeLauncherC 是 ini 自定义单位，[action_launchNuke] 带坐标目标，
    //   与 train 同一套 setAction(c, action, x, y) 通道（e.k=动作, e.l=坐标），
    //   下发后 custom/j.java:3008 把动作+坐标入队，完成时 fireTurretXAtGround
    //   朝坐标发射（custom/j.java:3214）。弹药=am.cE（int，ini 键 ammo；cB 是 energy 勿混）
    //   （[action_buildNuke] addResources: ammo=1 造弹，launchNuke price: ammo=1 耗弹）。

    private static void handleLoad(l engine, n team, String line, String cmd) {
        Float tid = numVal(line, "target");
        String csv = strVal(line, "units");
        if (tid == null || csv == null || csv.trim().isEmpty()) {
            reply(false, cmd, "missing target/units");
            return;
        }
        am trans = w.a(tid.longValue(), false);
        if (trans == null || trans.bV) {
            reply(false, cmd, "运输舰不存在/已摧毁");
            return;
        }
        if (trans.bX != team) {
            reply(false, cmd, "运输舰不是我方的");
            return;
        }
        if (!(trans instanceof ak) || !trans.cr()) {
            reply(false, cmd, "target #" + trans.eh + " 不是运输单位（hovercraft/dropship/experimentalDropship 及其 ini 系）");
            return;
        }
        HashSet<Long> want = new HashSet<Long>();
        for (String s : csv.split(",")) {
            s = s.trim();
            if (s.isEmpty()) {
                continue;
            }
            try {
                want.add(Long.parseLong(s));
            } catch (NumberFormatException ignored) {
            }
        }
        // 被装单位沿用 group 指令口径：委托池 ∩ 指定 id，只动我方
        ArrayList<am> cargo = new ArrayList<am>();
        int skipped = 0;
        StringBuilder skipWhy = new StringBuilder();
        for (am u : delegatedUnits(team)) {
            if (!want.contains(u.eh)) {
                continue;
            }
            want.remove(u.eh);
            if (u == trans || u.bV) {
                skipped++;
                continue;
            }
            if (!(u instanceof y)) {
                skipped++;
                continue;
            }
            if (u.cN != null) {
                if (skipWhy.length() > 0) {
                    skipWhy.append(',');
                }
                skipWhy.append("#").append(u.eh).append("已在载具中");
                skipped++;
                continue;
            }
            if (!trans.d(u, false)) {
                if (skipWhy.length() > 0) {
                    skipWhy.append(',');
                }
                skipWhy.append("#").append(u.eh).append("装不下(容量/类型/队伍不符)");
                skipped++;
                continue;
            }
            cargo.add(u);
        }
        if (cargo.isEmpty()) {
            reply(false, cmd, "没有可装载的委托单位（共" + skipped + "个被跳过"
                + (skipWhy.length() > 0 ? ": " + skipWhy : "")
                + (want.isEmpty() ? "" : "；" + want.size() + "个id不在委托池/不存在") + "）");
            return;
        }
        e c = engine.cf.b(team);
        for (am u : cargo) {
            c.a((y) u);
        }
        c.e(trans); // av.n 进入运输舰路点：与右键点运输舰完全同一条命令
        reply(true, cmd, cargo.size() + " 单位装载 → " + trans.r().i() + "#" + trans.eh
            + "（引擎右键装载路点，单位自行走到运输舰旁进入）"
            + (skipped > 0 ? "（" + skipped + "个被跳过: " + skipWhy + "）" : ""));
    }

    private static void handleUnload(l engine, n team, String line, String cmd) throws Exception {
        if (!ensureActionReflect()) {
            reply(false, cmd, "action reflection unavailable");
            return;
        }
        Float tid = numVal(line, "target");
        Float x = numVal(line, "x");
        Float yv = numVal(line, "y");
        if (tid == null || x == null || yv == null) {
            reply(false, cmd, "missing target/x/y");
            return;
        }
        am trans = w.a(tid.longValue(), false);
        if (trans == null || trans.bV) {
            reply(false, cmd, "运输舰不存在/已摧毁");
            return;
        }
        if (trans.bX != team) {
            reply(false, cmd, "运输舰不是我方的");
            return;
        }
        if (!(trans instanceof y)) {
            reply(false, cmd, "运输舰不可指挥");
            return;
        }
        ak t = (trans instanceof ak && trans.cr()) ? (ak) trans : null;
        if (t == null) {
            reply(false, cmd, "target #" + trans.eh + " 不是运输单位");
            return;
        }
        if (t.bB() <= 0) {
            reply(false, cmd, trans.r().i() + "#" + trans.eh + " 空仓，没有可卸单位");
            return;
        }
        // ① 先移动到卸货点；② 再下 unload 动作（am.cp() → ActionId("109")，
        // 引擎每 30 tick 放下一单位，停下即卸）。与 AI 卸货流程同两条命令
        // （game/a/n.java:297 move + :320 unload）。
        e c1 = engine.cf.b(team);
        c1.a((y) trans);
        c1.a(x.floatValue(), yv.floatValue());
        e c2 = engine.cf.b(team);
        c2.a((y) trans);
        Object unloadAct = invokePub(trans, "cp", new Class<?>[0]);
        eSetAction.invoke(c2, unloadAct);
        reply(true, cmd, trans.r().i() + "#" + trans.eh + " 前往(" + x.intValue() + "," + yv.intValue()
            + ")卸货，载员 " + t.bB() + (t.bA() ? "（已在卸货中，命令会续上）" : ""));
    }

    private static void handleLaunch(l engine, n team, String line, String cmd) throws Exception {
        if (!ensureActionReflect()) {
            reply(false, cmd, "action reflection unavailable");
            return;
        }
        Float id = numVal(line, "id");
        Float x = numVal(line, "x");
        Float yv = numVal(line, "y");
        if (id == null || x == null || yv == null) {
            reply(false, cmd, "missing id/x/y");
            return;
        }
        am silo = w.a(id.longValue(), false);
        if (silo == null || silo.bV) {
            reply(false, cmd, "发射井不存在/已摧毁");
            return;
        }
        if (silo.bX != team) {
            reply(false, cmd, "发射井不是我方的");
            return;
        }
        if (!(silo instanceof y)) {
            reply(false, cmd, "发射井不可指挥");
            return;
        }
        if (silo.cm < 1.0f) {
            reply(false, cmd, "发射井尚未完工（" + (int) (silo.cm * 100) + "%）");
            return;
        }
        String action = resolveActionOn(silo, "action_launchNuke");
        if (action == null) {
            // 引擎动作表用位置 id（实机回执"可用: _1,_0"）；unit_actions.json ini 序实据
            // launchNuke 是第 2 个动作 = _1。仅服务核弹井族，换单位变体需复测。
            action = resolveActionOn(silo, "_1");
        }
        if (action == null) {
            reply(false, cmd, "单位#" + silo.eh + " 没有 launchNuke 动作（不是核弹井）");
            dumpActions(silo, "launch");
            return;
        }
        Object actOnSilo = lookupAction(silo, action);
        Boolean avail = (Boolean) invokePub(actOnSilo, "b", new Class<?>[]{am.class}, silo);
        if (avail != null && !avail) {
            reply(false, cmd, "发射动作当前不可用（科技/条件不满足）");
            return;
        }
        // 反编译实据 custom/d/b.java:444-478——ini 资源键映射：energy→am.cB（浮点能量）、
        // ammo→am.cE（整型弹药）。buildNuke addResources ammo=1 写 cE，launchNuke price ammo=1 扣 cE。
        // 旧版误读 cB（能量）致"没有核弹"恒假阴。2026-10-20 实弹修。
        float ammo = silo.cE;
        if (ammo < 1.0f) {
            reply(false, cmd, "没有核弹（ammo=0）：先 train 造弹 action_buildNuke（$11000）");
            return;
        }
        double dist = Math.hypot(x.doubleValue() - silo.eo, yv.doubleValue() - silo.ep);
        if (dist < 260.0) {
            reply(false, cmd, "目标点离发射井太近（" + (int) dist + "<260）：核弹最小射程 260"
                + "（stock ini turret_siloTop limitingMinRange），游戏会静默拒绝");
            return;
        }
        e c = engine.cf.b(team);
        c.a((y) silo);
        setAction(c, action, x, yv); // e.k=动作id, e.l=坐标 → 与 train 同一通道，带坐标目标
        reply(true, cmd, "nuke launch @(" + x.intValue() + "," + yv.intValue() + ")，剩余弹药 "
            + (int) ammo + "（飞行约数秒，落点范围伤害）");
    }

    /** "s_extractorT1"/"b_extractorT1"/"u_tank" -> 类型名；纯数字 id（升级动作）返回 null。 */
    private static as resolveType(String action) {
        if (action == null || action.length() < 3) {
            return null;
        }
        char prefix = action.charAt(0);
        if ((prefix != 's' && prefix != 'b' && prefix != 'u') || action.charAt(1) != '_') {
            return null;
        }
        String name = action.substring(2);
        if (ar.ae == null) {
            return null;
        }
        for (Object o : ar.ae) {
            if (o instanceof as) {
                as t = (as) o;
                try {
                    if (name.equals(t.i())) {
                        return t;
                    }
                } catch (Throwable ignored) {
                }
            }
        }
        return null;
    }

    /** 在给定 id、前缀变体、以及模糊唯一匹配中，返回该单位实际拥有的动作 id；都没有则返回 null。 */
    private static String resolveActionOn(am u, String action) throws Exception {
        if (lookupAction(u, action) != null) {
            return action;
        }
        for (String prefix : new String[]{"s_", "b_", "u_"}) {
            if (action.startsWith(prefix)) {
                String rest = action.substring(2);
                for (String alt : new String[]{"s_", "b_", "u_"}) {
                    if (!alt.equals(prefix) && lookupAction(u, alt + rest) != null) {
                        return alt + rest;
                    }
                }
                break;
            }
        }
        // 模糊匹配：LLM 爱用缩写（如 s_mech），若该单位动作表里只有一个包含该串的 id 则自动纠正
        String bare = action.toLowerCase();
        int us = bare.indexOf('_');
        if (us >= 0) {
            bare = bare.substring(us + 1);
        }
        if (bare.length() < 3) {
            return null;
        }
        String match = null;
        int count = 0;
        for (Object a : u.N()) {
            try {
                String id = (String) invokePub(a, "O", new Class<?>[0]);
                if (id == null) {
                    continue;
                }
                if (id.toLowerCase().contains(bare)) {
                    match = id;
                    count++;
                }
            } catch (Throwable ignored) {
            }
        }
        return count == 1 ? match : null;
    }

    private static Object lookupAction(am u, String actionId) throws Exception {
        Object actId = actionIdOf.invoke(null, actionId);
        return invokePub(u, "a", new Class<?>[]{actionIdClass}, actId);
    }

    private static String availableActions(am u, int cap) {
        StringBuilder sb = new StringBuilder();
        try {
            int n = 0;
            for (Object a : u.N()) {
                try {
                    String id = (String) invokePub(a, "O", new Class<?>[0]);
                    if (id == null || id.isEmpty() || id.charAt(0) == '<') {
                        continue;
                    }
                    if (sb.length() > 0) {
                        sb.append(',');
                    }
                    sb.append(id);
                    if (++n >= cap) {
                        break;
                    }
                } catch (Throwable ignored) {
                }
            }
        } catch (Throwable ignored) {
        }
        return sb.toString();
    }

    /** 单个单位能否按自身移动类型到达 (x,y)（连通性检查，游戏寻路同款隔离组数据）。 */
    private static boolean canReach(am u, float x, float y) {
        try {
            if (!(u instanceof y)) {
                return true; // 非机动单位（建筑等）不挡
            }
            ao mt = u.h();
            if (mt == null || mt == ao.a || mt == ao.c || mt == ao.d) {
                return true;
            }
            return com.corrodinggames.rts.gameFramework.utility.y.a(u.eo, u.ep, x, y, mt);
        } catch (Throwable t) {
            return true; // 检查失败不挡指令
        }
    }

    /** 任一单位能否按自身移动类型到达 (x,y)（连通性检查，游戏寻路同款隔离组数据）。 */
    private static boolean anyReachable(ArrayList<am> units, float x, float y) {
        for (am u : units) {
            if (!(u instanceof y)) {
                continue;
            }
            try {
                ao mt = u.h();
                if (mt == null || mt == ao.a || mt == ao.c || mt == ao.d) {
                    return true; // NONE/BUILDING/AIR 不做连通性检查
                }
                if (com.corrodinggames.rts.gameFramework.utility.y.a(u.eo, u.ep, x, y, mt)) {
                    return true;
                }
            } catch (Throwable t) {
                return true; // 检查失败不挡指令
            }
        }
        return false;
    }

    /** 动作预检失败时，把该单位实际可用的动作 id 写进日志（排查用）。 */
    private static void dumpActions(am u, String context) {
        try {
            StringBuilder sb = new StringBuilder();
            sb.append("actions on ").append(u.r().i()).append(" [").append(context).append("]: ");
            java.util.ArrayList acts = u.N();
            for (Object a : acts) {
                try {
                    sb.append((String) invokePub(a, "O", new Class<?>[0])).append(',');
                } catch (Throwable ignored) {
                }
            }
            Log.info(sb.toString());
        } catch (Throwable t) {
            Log.error("dumpActions failed", t);
        }
    }

    /** Easy Build 选址：资源池类→离锚点最近的未被占池点；其他→锚点螺旋搜索首个通过权威校验的点。
     *  池点坐标只做近似，实际落点用 9 宫格候选交给权威校验自纠（y.b() 说了算）。
     *  失败时 reason 里写入诊断（各池被占数/校验错误码/候选数）。 */
    private static final int[][] POOL_CANDIDATES = {
        {0, 0}, {10, 0}, {-10, 0}, {0, 10}, {0, -10}, {10, 10}, {-10, -10}, {10, -10}, {-10, 10}
    };

    /** 池点"被占"判定半径：只要池心附近有建筑即视为已占用（100 误伤太大，曾致无池可建）。 */
    private static final float POOL_BLOCK_RADIUS = 40f;

    /** 离锚点最近的前 cap 个空闲池坐标，用于失败回执引导换点。 */
    private static String freePoolsText(l engine, float ax, float ay, int cap) {
        try {
            java.util.ArrayList<float[]> pools = resourcePoints(engine);
            pools.sort((a, b) -> Float.compare(dist2(a, ax, ay), dist2(b, ax, ay)));
            StringBuilder sb = new StringBuilder();
            int n = 0;
            for (float[] p : pools) {
                if (blockedByBuilding(p[0], p[1], POOL_BLOCK_RADIUS)) {
                    continue;
                }
                if (sb.length() > 0) {
                    sb.append(',');
                }
                sb.append('(').append((int) p[0]).append(',').append((int) p[1]).append(')');
                if (++n >= cap) {
                    break;
                }
            }
            return sb.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    private static float[] findBuildSpot(l engine, n team, as type, float ax, float ay, StringBuilder reason) {
        return findBuildSpot(engine, team, type, ax, ay, reason, null);
    }

    /** 带建造者的选址：池点过滤时同时排除建造者无法靠近的（水域/隔断），避免选了对岸的池反复撞墙。 */
    private static float[] findBuildSpot(l engine, n team, as type, float ax, float ay, StringBuilder reason, am builder) {
        try {
            if (type.p()) {
                java.util.ArrayList<float[]> pools = resourcePoints(engine);
                pools.sort((a, b) -> Float.compare(dist2(a, ax, ay), dist2(b, ax, ay)));
                int blocked = 0;
                int invalid = 0;
                int unreachable = 0;
                String firstErr = null;
                float[] firstPool = pools.isEmpty() ? null : pools.get(0);
                for (float[] p : pools) {
                    if (blockedByBuilding(p[0], p[1], POOL_BLOCK_RADIUS)) {
                        blocked++;
                        continue;
                    }
                    if (builder != null && !builderCanApproach(builder, p[0], p[1])) {
                        unreachable++;
                        continue;
                    }
                    boolean ok = false;
                    for (int[] off : POOL_CANDIDATES) {
                        float cx = p[0] + off[0];
                        float cy = p[1] + off[1];
                        if (placementError(type, cx, cy, team) == null) {
                            return new float[]{cx, cy};
                        }
                    }
                    invalid++;
                    if (firstErr == null) {
                        firstErr = "九宫格全失败@(" + (int) p[0] + "," + (int) p[1] + ")";
                    }
                }
                reason.append("资源池共").append(pools.size()).append("个：被占").append(blocked)
                      .append("，无法靠近").append(unreachable)
                      .append("，校验不过").append(invalid)
                      .append(firstErr == null ? "" : "，" + firstErr)
                      .append(firstPool == null ? "" : "，最近池(" + (int) firstPool[0] + "," + (int) firstPool[1] + ")");
                return null;
            }
            float[] radii = {0f, 80f, 140f, 210f, 290f, 380f, 500f};
            int tried = 0;
            String firstErr = null;
            for (float r : radii) {
                for (int k = 0; k < 8; k++) {
                    double ang = k * Math.PI / 4.0;
                    float cx = ax + (float) (Math.cos(ang) * r);
                    float cy = ay + (float) (Math.sin(ang) * r);
                    if (blockedByBuilding(cx, cy, 55f)) {
                        continue;
                    }
                    String err = placementError(type, cx, cy, team);
                    tried++;
                    if (err == null) {
                        return new float[]{cx, cy};
                    }
                    if (firstErr == null) {
                        firstErr = placementReason(err);
                    }
                }
            }
            reason.append("螺旋选址尝试").append(tried).append("点全失败，首个错误")
                  .append(firstErr == null ? "(全被占)" : firstErr);
            return null;
        } catch (Throwable t) {
            reason.append("选址异常:").append(t);
            return null;
        }
    }

    private static float dist2(float[] p, float ax, float ay) {
        float dx = p[0] - ax;
        float dy = p[1] - ay;
        return dx * dx + dy * dy;
    }

    /** 地图资源点：吸附到所在 tile 的中心（与玩家手动建造的落点一致，(tileX+0.5)*tileW）。 */
    private static java.util.ArrayList<float[]> resourcePoints(l engine) {
        java.util.ArrayList<float[]> out = new java.util.ArrayList<float[]>();
        try {
            Object map = engine.bL;
            Class<?> mc = map.getClass();
            java.util.ArrayList<?> res = (java.util.ArrayList<?>) mc.getField("A").get(map);
            if (res == null) {
                return out;
            }
            float tsx = 1.0f / mc.getField("r").getFloat(map);
            float tsy = 1.0f / mc.getField("s").getFloat(map);
            java.lang.reflect.Method toWorld = null;
            java.lang.reflect.Field fx = null;
            java.lang.reflect.Field fy = null;
            for (Object p : res) {
                if (p == null) {
                    continue;
                }
                if (toWorld == null) {
                    toWorld = mc.getMethod("a", p.getClass());
                }
                Object pf = toWorld.invoke(map, p);
                if (pf == null) {
                    continue;
                }
                if (fx == null) {
                    fx = pf.getClass().getField("a");
                    fy = pf.getClass().getField("b");
                }
                float px = fx.getFloat(pf);
                float py = fy.getFloat(pf);
                // 对齐 tile 中心
                float cx = (float) ((Math.floor(px / tsx) + 0.5) * tsx);
                float cy = (float) ((Math.floor(py / tsy) + 0.5) * tsy);
                out.add(new float[]{cx, cy});
            }
        } catch (Throwable ignored) {
        }
        return out;
    }

    /** 权威放置校验（y.b(false,team)，y.java:5175）：null=合法，否则 {0}/{2}/{3}。 */
    private static String placementError(as type, float x, float y, n team) {
        try {
            am sample = am.a(type); // 共享样板（am.java:535，勿新建）
            if (!(sample instanceof y)) {
                return null;
            }
            float ox = sample.eo;
            float oy = sample.ep;
            try {
                sample.eo = x;
                sample.ep = y;
                return ((y) sample).b(false, team);
            } finally {
                sample.eo = ox;
                sample.ep = oy;
            }
        } catch (Throwable t) {
            return null; // 校验失败不挡指令
        }
    }

    /** 放置模板码 → 人话原因（y.java:5189 {2}=不在资源池；g.java:1982/1998 {0}=占位/碰撞/超范围）。 */
    private static String placementReason(String err) {
        if (err == null) {
            return "?";
        }
        switch (err) {
            case "{0}":
                return "占位/碰撞或超建造范围";
            case "{1}":
                return "条件不符(游戏自定义)";
            case "{2}":
                return "不在资源池上";
            case "{3}":
                return "地形不符(水/岩浆等)";
            default:
                return err;
        }
    }

    /** buildq 池点吸附：找离目标最近的资源池（>60px 视为指错地方），
     *  用 9 宫格候选交权威校验自纠（与单条 build 的 findBuildSpot 同套）。
     *  游戏对非池心的采集器落点会静默丢路点，必须先吸附。 */
    private static float[] snapToPool(l engine, n team, as type, float px, float py) {
        try {
            float[] best = null;
            float bestD = Float.MAX_VALUE;
            for (float[] p : resourcePoints(engine)) {
                float d = dist2(p, px, py);
                if (d < bestD) {
                    bestD = d;
                    best = p;
                }
            }
            if (best == null || bestD > 60f * 60f) {
                return null;
            }
            for (int[] off : POOL_CANDIDATES) {
                float cx = best[0] + off[0];
                float cy = best[1] + off[1];
                if (placementError(type, cx, cy, team) == null) {
                    return new float[]{cx, cy};
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    /** 目标不可达时，在目标周围环形搜索一个"从部队和到目标都可达"的绕行点，写进回执引导绕行。 */
    private static String findDetour(ArrayList<am> units, float tx, float ty) {
        try {
            for (am u : units) {
                if (!(u instanceof y)) {
                    continue;
                }
                ao mt = u.h();
                if (mt == null || mt == ao.a || mt == ao.c || mt == ao.d) {
                    continue;
                }
                float[] radii = {300f, 600f, 1000f, 1500f, 2200f, 3000f};
                for (float r : radii) {
                    for (int k = 0; k < 8; k++) {
                        double ang = k * Math.PI / 4.0;
                        float cx = tx + (float) (Math.cos(ang) * r);
                        float cy = ty + (float) (Math.sin(ang) * r);
                        boolean toUnit = com.corrodinggames.rts.gameFramework.utility.y.a(u.eo, u.ep, cx, cy, mt);
                        boolean toTarget = com.corrodinggames.rts.gameFramework.utility.y.a(cx, cy, tx, ty, mt);
                        if (toUnit && toTarget) {
                            return "可先 move 到(" + (int) cx + "," + (int) cy + ")绕行（该点部队与目标双向可达）";
                        }
                    }
                }
                break; // 只用第一个机动单位测
            }
        } catch (Throwable ignored) {
        }
        return "未找到绕行点，换个目标或换兵种";
    }

    /** 建造者能否靠近目标（目标点或 85px 环带任一点可达即放行，岸边造水上建筑合法）。 */
    private static boolean builderCanApproach(am builder, float x, float y) {
        try {
            ao bmt = builder.h();
            if (bmt == null || bmt == ao.a || bmt == ao.c || bmt == ao.d) {
                return true;
            }
            boolean ok = com.corrodinggames.rts.gameFramework.utility.y.a(
                builder.eo, builder.ep, x, y, bmt);
            for (int k = 0; k < 8 && !ok; k++) {
                double ang = k * Math.PI / 4.0;
                ok = com.corrodinggames.rts.gameFramework.utility.y.a(
                    builder.eo, builder.ep,
                    x + (float) (Math.cos(ang) * 85.0), y + (float) (Math.sin(ang) * 85.0), bmt);
            }
            return ok;
        } catch (Throwable t) {
            return true; // 检查失败不挡指令
        }
    }

    /** 选址默认锚点：优先指挥中心；无指挥中心（部分开局）回退到首个建造者。 */
    private static am findCommandCenter(n team) {
        am firstBuilder = null;
        try {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (obj instanceof am) {
                    am u = (am) obj;
                    if (!u.bV && !u.ej && u.bX == team) {
                        if ("commandCenter".equals(u.r().i())) {
                            return u;
                        }
                        if (firstBuilder == null) {
                            try {
                                if (u.r().l()) {
                                    firstBuilder = u;
                                }
                            } catch (Throwable ignored) {
                            }
                        }
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return firstBuilder;
    }
    /** 目标位置 radius 内是否已有建筑（任意队伍的建筑都算阻挡）。 */
    private static boolean blockedByBuilding(float x, float y) {
        return blockedByBuilding(x, y, 55f);
    }

    private static boolean blockedByBuilding(float x, float y, float radius) {
        try {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (!(obj instanceof am)) {
                    continue;
                }
                am u = (am) obj;
                if (u.bV || u.ej) {
                    continue;
                }
                try {
                    if (!u.r().j()) {
                        continue; // 只挡建筑
                    }
                } catch (Throwable ignored) {
                    continue;
                }
                float d = (float) Math.hypot(u.eo - x, u.ep - y);
                if (d < radius) {
                    return true;
                }
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    // ---- delegated units & groups ----

    private static int addDelegated(e c, n team, String line) {
        int count = 0;
        boolean hasIds = strVal(line, "units") != null;
        ArrayList<am> units = filterByIds(delegatedUnits(team), line);
        for (am u : units) {
            if (!hasIds && isBuilding(u)) {
                continue; // stop/attack 默认不带建筑（除非 ids 显式点名）
            }
            if (!hasIds) {
                try {
                    if (u.r().l()) {
                        continue; // 战斗指令不拖建造者上火线
                    }
                } catch (Throwable ignored) {
                }
            }
            if (u instanceof y) {
                c.a((y) u);
                count++;
            }
        }
        return count;
    }

    /** 是否建筑（type.j()）；判定失败按非建筑处理，不挡指令。 */
    private static boolean isBuilding(am u) {
        try {
            return u.r().j();
        } catch (Throwable t) {
            return false;
        }
    }

    /** 委托单位：全队模式 → 全部我方单位；否则 id 池 ∪ 委托编队槽。Game thread only. */
    static ArrayList<am> delegatedUnits(n team) {
        ArrayList<am> out = new ArrayList<am>();
        if (AiState.controlAll) {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (obj instanceof am) {
                    am u = (am) obj;
                    if (!u.bV && !u.ej && u.bX == team) {
                        out.add(u);
                    }
                }
            }
            return out;
        }
        if (!AiState.delegatedIds.isEmpty()) {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (obj instanceof am) {
                    am u = (am) obj;
                    if (!u.bV && !u.ej && u.bX == team && AiState.delegatedIds.contains(u.eh)) {
                        out.add(u);
                    }
                }
            }
        }
        ArrayList<?> groups = groups();
        if (groups != null) {
            for (int slot : delegatedSlots) {
                if (slot >= groups.size()) {
                    continue;
                }
                collectSlot(team, groups.get(slot), out);
            }
        }
        return out;
    }

    /** [委托选中]：把玩家当前框选的我方单位加入委托池。返回新增数量。 */
    static int pickSelection(n team) {
        int added = 0;
        try {
            long[] sel = lastSelection;
            for (long id : sel) {
                am u = w.a(id, true);
                if (u != null && !u.bV && !u.ej && u.bX == team && AiState.delegatedIds.add(id)) {
                    added++;
                }
            }
        } catch (Throwable t) {
            Log.error("pickSelection failed", t);
        }
        return added;
    }

    /** 最后一次看到的非空选择集（点击按钮会清选择，必须用缓存）。TickLoop 每 tick 更新。 */
    static volatile long[] lastSelection = new long[0];

    static void trackSelection(n team) {
        try {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            long[] cur = new long[64];
            int n = 0;
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (obj instanceof am) {
                    am u = (am) obj;
                    if (u.cG && !u.bV && !u.ej && u.bX == team) {
                        if (n == cur.length) {
                            long[] bigger = new long[cur.length * 2];
                            System.arraycopy(cur, 0, bigger, 0, cur.length);
                            cur = bigger;
                        }
                        cur[n++] = u.eh;
                    }
                }
            }
            if (n > 0) {
                long[] out = new long[n];
                System.arraycopy(cur, 0, out, 0, n);
                lastSelection = out;
            }
        } catch (Throwable ignored) {
        }
    }

    /** 指令里的 "units":"id1,id2" 过滤；无该字段返回原列表。 */
    private static ArrayList<am> filterByIds(ArrayList<am> units, String line) {
        String csv = strVal(line, "units");
        if (csv == null || csv.isEmpty()) {
            return units;
        }
        HashSet<Long> ids = new HashSet<Long>();
        for (String s : csv.split(",")) {
            s = s.trim();
            if (s.isEmpty()) {
                continue;
            }
            try {
                ids.add(Long.parseLong(s));
            } catch (NumberFormatException ignored) {
            }
        }
        if (ids.isEmpty()) {
            return units;
        }
        ArrayList<am> out = new ArrayList<am>();
        for (am u : units) {
            if (ids.contains(u.eh)) {
                out.add(u);
            }
        }
        return out;
    }

    static synchronized void setDelegatedSlots(Set<Integer> slots) {
        delegatedSlots = Collections.unmodifiableSet(new HashSet<Integer>(slots));
        Log.info("delegated slots -> " + slots);
    }

    /** 用户编队号（1-9,0）形式的委托列表，用于显示与通知。 */
    static String delegatedSlotsString() {
        java.util.TreeSet<Integer> user = new java.util.TreeSet<Integer>();
        for (int slot : delegatedSlots) {
            user.add(slot == 9 ? 0 : slot + 1);
        }
        StringBuilder sb = new StringBuilder();
        for (int g : user) {
            if (sb.length() > 0) {
                sb.append(',');
            }
            sb.append(g);
        }
        return sb.length() == 0 ? "(无)" : sb.toString();
    }

    /** JSON summary of all non-empty control groups: [{slot,count,types}] */
    static String groupsSummary(n team) {
        ArrayList<?> groups = groups();
        if (groups == null) {
            return "[]";
        }
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (int slot = 0; slot < groups.size() && slot < 10; slot++) {
            ArrayList<am> members = new ArrayList<am>();
            collectSlot(team, groups.get(slot), members);
            if (members.isEmpty()) {
                continue;
            }
            LinkedHashMap<String, Integer> counts = new LinkedHashMap<String, Integer>();
            for (am u : members) {
                try {
                    String t = u.r().i();
                    Integer v = counts.get(t);
                    counts.put(t, v == null ? 1 : v + 1);
                } catch (Throwable ignored) {
                }
            }
            StringBuilder types = new StringBuilder();
            int shown = 0;
            for (Map.Entry<String, Integer> en : counts.entrySet()) {
                if (shown++ >= 3) {
                    types.append(",…");
                    break;
                }
                if (types.length() > 0) {
                    types.append(',');
                }
                types.append(en.getKey()).append('x').append(en.getValue());
            }
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append("{\"slot\":").append(slot)
              .append(",\"count\":").append(members.size())
              .append(",\"types\":\"").append(types).append("\"")
              .append(",\"delegated\":").append(delegatedSlots.contains(slot))
              .append('}');
        }
        return sb.append(']').toString();
    }

    private static void collectSlot(n team, Object grp, ArrayList<am> out) {
        if (grp == null || !ensureReflect()) {
            return;
        }
        try {
            ArrayList<?> members = (ArrayList<?>) groupMembers.get(grp);
            if (members == null) {
                return;
            }
            for (Object o : members) {
                if (o instanceof am) {
                    am u = (am) o;
                    if (!u.bV && !u.ej && u.bX == team) {
                        out.add(u);
                    }
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private static ArrayList<?> groups() {
        if (!ensureReflect()) {
            return null;
        }
        try {
            Object hud = l.B().bS;
            if (hud == null) {
                return null;
            }
            Object fa = hudControlGroups.get(hud);
            if (fa == null) {
                return null;
            }
            return (ArrayList<?>) groupsList.get(fa);
        } catch (Throwable t) {
            return null;
        }
    }

    private static synchronized boolean ensureReflect() {
        if (reflectInit) {
            return hudControlGroups != null;
        }
        reflectInit = true;
        try {
            Class<?> fg = Class.forName("com.corrodinggames.rts.gameFramework.f.g");
            hudControlGroups = fg.getDeclaredField("g");
            hudControlGroups.setAccessible(true);
            Class<?> fa = Class.forName("com.corrodinggames.rts.gameFramework.f.a");
            groupsList = fa.getDeclaredField("aA");
            groupsList.setAccessible(true);
            Class<?> fam = Class.forName("com.corrodinggames.rts.gameFramework.f.am");
            groupMembers = fam.getDeclaredField("a");
            groupMembers.setAccessible(true);
            Log.info("control-group reflection ok");
        } catch (Throwable t) {
            hudControlGroups = null;
            Log.error("control-group reflection failed", t);
        }
        return hudControlGroups != null;
    }

    // ---- reply & minimal JSON helpers ----

    static void reply(boolean ok, String cmd, String msg) {
        Log.info("cmd " + cmd + (ok ? " ok: " : " FAIL: ") + msg);
        NetServer.send("{\"type\":\"cmdResult\",\"ok\":" + ok + ",\"cmd\":\"" + esc(cmd)
            + "\",\"msg\":\"" + esc(msg) + "\"}");
    }

    static String strVal(String json, String key) {
        String pat = "\"" + key + "\"";
        int i = json.indexOf(pat);
        if (i < 0) {
            return null;
        }
        int colon = json.indexOf(':', i + pat.length());
        if (colon < 0) {
            return null;
        }
        int q1 = json.indexOf('"', colon + 1);
        if (q1 < 0) {
            return null;
        }
        // 找未转义的结束引号，并做 JSON 字符串反转义
        StringBuilder sb = new StringBuilder();
        for (int j = q1 + 1; j < json.length(); j++) {
            char c = json.charAt(j);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\' && j + 1 < json.length()) {
                char n = json.charAt(j + 1);
                if (n == 'u' && j + 5 < json.length()) {
                    try {
                        sb.append((char) Integer.parseInt(json.substring(j + 2, j + 6), 16));
                        j += 5;
                        continue;
                    } catch (NumberFormatException ignored) {
                    }
                } else if (n == 'n') {
                    sb.append('\n');
                    j++;
                    continue;
                } else if (n == 't') {
                    sb.append('\t');
                    j++;
                    continue;
                } else if (n == '"' || n == '\\' || n == '/') {
                    sb.append(n);
                    j++;
                    continue;
                }
            }
            sb.append(c);
        }
        return null;
    }

    static Float numVal(String json, String key) {
        String pat = "\"" + key + "\"";
        int i = json.indexOf(pat);
        if (i < 0) {
            return null;
        }
        int colon = json.indexOf(':', i + pat.length());
        if (colon < 0) {
            return null;
        }
        int j = colon + 1;
        while (j < json.length() && (json.charAt(j) == ' ' || json.charAt(j) == '\t')) {
            j++;
        }
        int k = j;
        while (k < json.length()) {
            char c = json.charAt(k);
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E') {
                k++;
            } else {
                break;
            }
        }
        if (k == j) {
            return null;
        }
        try {
            return Float.valueOf(json.substring(j, k));
        } catch (NumberFormatException ex) {
            return null;
        }
    }

    static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 32) {
                sb.append('?');
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }
}
