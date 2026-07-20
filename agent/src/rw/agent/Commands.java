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
 *   {"cmd":"overlay","text":"line1\\nline2"}   (handled by OverlayData)
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
                // 逐单位过滤：不可达的排除；整队指令自动跳过施工中的单位（除非 ids 显式点名）
                ArrayList<am> targets = filterByIds(delegatedUnits(team), line);
                ArrayList<am> reach = new ArrayList<am>();
                int excluded = 0;
                int busy = 0;
                boolean hasIds = strVal(line, "units") != null;
                for (am u : targets) {
                    if (!hasIds) {
                        String act = Activity.describe(u);
                        if (act != null && act.startsWith("build")) {
                            busy++;
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
                    reply(false, cmd, "目标点对全部受令单位不可达（被水/岩浆/高崖隔断），换个目标或换兵种");
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
                    + (busy > 0 ? "（" + busy + "个施工中已跳过）" : ""));
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
        // 施工保护：目标建造者已有施工任务时拒绝（防顶掉原施工）
        try {
            String bAct = Activity.describe(builder);
            if (bAct != null && bAct.startsWith("build")) {
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
        float[] spot = findBuildSpot(engine, team, type, ax, ay, spotReason);
        if (spot == null) {
            reply(false, cmd, "找不到合法建造位置：" + spotReason);
            return;
        }
        x = spot[0];
        yv = spot[1];
        // 可达性预检：目标 85px 环带采样——建造者能走到施工距离内任一点即放行（岸边造水上建筑合法）
        boolean reachable = true;
        try {
            ao bmt = builder.h();
            if (bmt != null && bmt != ao.a && bmt != ao.c && bmt != ao.d) {
                reachable = com.corrodinggames.rts.gameFramework.utility.y.a(
                    builder.eo, builder.ep, x.floatValue(), yv.floatValue(), bmt);
                for (int k = 0; k < 8 && !reachable; k++) {
                    double ang = k * Math.PI / 4.0;
                    float sx = x.floatValue() + (float) (Math.cos(ang) * 85.0);
                    float sy = yv.floatValue() + (float) (Math.sin(ang) * 85.0);
                    reachable = com.corrodinggames.rts.gameFramework.utility.y.a(
                        builder.eo, builder.ep, sx, sy, bmt);
                }
            }
        } catch (Throwable ignored) {
        }
        if (!reachable) {
            reply(false, cmd, "建造者无法靠近目标85px内（水域/隔断），需换海上建造者或换位置");
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
        Boolean availB = (Boolean) actOnB.getClass().getMethod("b", am.class).invoke(actOnB, builder);
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
            reply(false, cmd, "该工厂没有此动作: " + action + (avail.isEmpty() ? "" : "；可用: " + avail));
            dumpActions(fac, "train " + action);
            return;
        }
        action = resolvedT;
        Object actOnUnit = lookupAction(fac, action);
        Boolean avail = (Boolean) actOnUnit.getClass().getMethod("b", am.class).invoke(actOnUnit, fac);
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
                String id = (String) a.getClass().getMethod("O").invoke(a);
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
        return u.getClass().getMethod("a", actionIdClass).invoke(u, actId);
    }

    private static String availableActions(am u, int cap) {
        StringBuilder sb = new StringBuilder();
        try {
            int n = 0;
            for (Object a : u.N()) {
                try {
                    String id = (String) a.getClass().getMethod("O").invoke(a);
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
                    sb.append((String) a.getClass().getMethod("O").invoke(a)).append(',');
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

    private static float[] findBuildSpot(l engine, n team, as type, float ax, float ay, StringBuilder reason) {
        try {
            if (type.p()) {
                java.util.ArrayList<float[]> pools = resourcePoints(engine);
                pools.sort((a, b) -> Float.compare(dist2(a, ax, ay), dist2(b, ax, ay)));
                int blocked = 0;
                int invalid = 0;
                String firstErr = null;
                float[] firstPool = pools.isEmpty() ? null : pools.get(0);
                for (float[] p : pools) {
                    if (blockedByBuilding(p[0], p[1], 100f)) {
                        blocked++;
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
                        firstErr = err;
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

    /** 找我方指挥中心（选址默认锚点）。 */
    private static am findCommandCenter(n team) {
        try {
            com.corrodinggames.rts.gameFramework.utility.o units = am.bF();
            Object[] arr = units.b();
            int size = units.size();
            for (int i = 0; i < size; i++) {
                Object obj = arr[i];
                if (obj instanceof am) {
                    am u = (am) obj;
                    if (!u.bV && !u.ej && u.bX == team && "commandCenter".equals(u.r().i())) {
                        return u;
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }
    private static float[] snapToResource(l engine, float x, float y) {
        try {
            Object map = engine.bL;
            if (map == null) {
                return null;
            }
            Class<?> mc = map.getClass();
            java.util.ArrayList<?> res = (java.util.ArrayList<?>) mc.getField("A").get(map);
            if (res == null) {
                return null;
            }
            int yOff = mc.getField("o").getInt(map); // 池点 Y 偏移
            java.lang.reflect.Method toWorld = null;
            java.lang.reflect.Field fx = null;
            java.lang.reflect.Field fy = null;
            float best = 80f;
            float[] out = null;
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
                float py = fy.getFloat(pf) + yOff;
                float d = (float) Math.hypot(px - x, py - y);
                if (d < best) {
                    best = d;
                    out = new float[]{px, py};
                }
            }
            return out;
        } catch (Throwable t) {
            return null;
        }
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
        ArrayList<am> units = filterByIds(delegatedUnits(team), line);
        for (am u : units) {
            if (u instanceof y) {
                c.a((y) u);
                count++;
            }
        }
        return count;
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
