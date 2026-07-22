package rw.agent;

import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;

import com.corrodinggames.rts.gameFramework.m.y;

/**
 * overlay 迷你 UI 框架：共享 Paint 调色板、面板骨架（半透明底 + 1px 描边 + 标题栏）、
 * 按钮绘制与命中测试工具，供 ChatPanel / DelegPanel / OverlayData 主栏复用。
 * - 全部静态；绘制方法仅渲染线程调用（Paints 懒初始化后只复用，绝不每帧 new）
 * - 字号随引擎 UI 密度（applyScale(cj)，与布局坐标 ×cj 同倍率；禁用 cX——那是相机缩放，会跟地图变焦），每帧几个 setter 而已
 * - 命中测试为纯数学，几何坐标一律未缩放值 ×cj 后与真实屏幕像素比较
 */
final class Ui {
    /** 标题栏高（未缩放坐标） */
    static final float TITLE_H = 22f;

    // ---- 调色板（渲染线程懒初始化，之后只读复用） ----
    static Paint panelBg;       // 面板底：半透明深色
    static Paint panelBorder;   // 1px 描边
    static Paint titleBg;       // 标题栏底
    static Paint titleText;     // 标题字（粗体）
    static Paint text;          // 正文
    static Paint dimText;       // 次要/占位文本
    static Paint btnBg;         // 按钮底
    static Paint btnBgOn;       // 按钮底（开/激活态）
    static Paint btnText;       // 按钮字
    static Paint inputBg;       // 输入框底
    static Paint caret;         // 输入光标
    static Paint statusBg;      // 状态行底板
    static Paint imgPaint;      // 单位图标绘制用（空属性）
    static Paint hpBack;        // 血条底（暗红）
    static Paint hpGood;        // 血条（绿）
    static Paint hpBad;         // 血条（红，受损>15%）
    static Paint roleCommander; // 指挥：紫
    static Paint roleBuild;     // 建造：黄
    static Paint roleCombat;    // 战斗：红
    static Paint rolePlayer;    // 玩家：绿
    static Paint roleSystem;    // 系统：灰

    private Ui() {
    }

    /** 渲染线程懒初始化（draw 路径开头调）；幂等。 */
    static void ensure() {
        if (panelBg != null) {
            return;
        }
        panelBg = new Paint();
        panelBg.a(200, 15, 18, 24);
        panelBorder = new Paint();
        panelBorder.a(120, 90, 120, 160);
        titleBg = new Paint();
        titleBg.a(220, 30, 34, 48);
        titleText = new Paint();
        titleText.a(255, 235, 238, 245);
        titleText.a(true);
        try {
            titleText.a(Typeface.a(Typeface.c, 1)); // 粗体
        } catch (Throwable ignored) {
        }
        text = new Paint();
        text.a(255, 225, 228, 235);
        text.a(true);
        dimText = new Paint();
        dimText.a(255, 145, 150, 160);
        dimText.a(true);
        btnBg = new Paint();
        btnBg.a(190, 30, 45, 60);
        btnBgOn = new Paint();
        btnBgOn.a(200, 45, 85, 65);
        btnText = new Paint();
        btnText.a(255, 255, 255, 255);
        btnText.a(true);
        inputBg = new Paint();
        inputBg.a(220, 10, 12, 18);
        caret = new Paint();
        caret.a(255, 230, 230, 230);
        statusBg = new Paint();
        statusBg.a(140, 0, 0, 0);
        imgPaint = new Paint();
        hpBack = new Paint();
        hpBack.a(200, 70, 25, 25);
        hpGood = new Paint();
        hpGood.a(220, 80, 200, 90);
        hpBad = new Paint();
        hpBad.a(230, 230, 60, 60);
        roleCommander = new Paint();
        roleCommander.a(255, 190, 130, 255);
        roleCommander.a(true);
        roleBuild = new Paint();
        roleBuild.a(255, 255, 220, 120);
        roleBuild.a(true);
        roleCombat = new Paint();
        roleCombat.a(255, 255, 120, 120);
        roleCombat.a(true);
        rolePlayer = new Paint();
        rolePlayer.a(255, 140, 255, 140);
        rolePlayer.a(true);
        roleSystem = new Paint();
        roleSystem.a(255, 160, 160, 160);
        roleSystem.a(true);
        applyScale(1f);
    }

    /** 每帧一次：字号随 cj（UI 密度）走（布局坐标也 ×cj，两者必须同倍率才对齐）。 */
    static void applyScale(float s) {
        if (s <= 0f) {
            s = 1f;
        }
        titleText.b(15f * s);
        text.b(13f * s);
        dimText.b(12f * s);
        btnText.b(14f * s);
        roleCommander.b(13f * s);
        roleBuild.b(13f * s);
        roleCombat.b(13f * s);
        rolePlayer.b(13f * s);
        roleSystem.b(13f * s);
    }

    // ---- 聊天角色着色/标签（渲染线程） ----

    static Paint rolePaint(String who) {
        if ("commander".equals(who)) {
            return roleCommander;
        }
        if ("build".equals(who)) {
            return roleBuild;
        }
        if ("combat".equals(who)) {
            return roleCombat;
        }
        if ("player".equals(who)) {
            return rolePlayer;
        }
        return roleSystem;
    }

    static String roleLabel(String who) {
        if ("commander".equals(who)) {
            return "指挥";
        }
        if ("build".equals(who)) {
            return "建造";
        }
        if ("combat".equals(who)) {
            return "战斗";
        }
        if ("player".equals(who)) {
            return "你";
        }
        return "系统";
    }

    // ---- 绘制原语（坐标均为已缩放的真实像素） ----

    static void rect(y g, float x, float y0, float w, float h, Paint p) {
        g.a(new RectF(x, y0, x + w, y0 + h), p);
    }

    /** 1px 描边（4 条直线，避免 Paint.Style 枚举名依赖）。 */
    static void border(y g, float x, float y0, float w, float h) {
        g.a(x, y0, x + w, y0, panelBorder);
        g.a(x, y0 + h, x + w, y0 + h, panelBorder);
        g.a(x, y0, x, y0 + h, panelBorder);
        g.a(x + w, y0, x + w, y0 + h, panelBorder);
    }

    static void panel(y g, float x, float y0, float w, float h) {
        rect(g, x, y0, w, h, panelBg);
        border(g, x, y0, w, h);
    }

    static void titleBar(y g, float x, float y0, float w, float h, float s, String title) {
        rect(g, x, y0, w, h, titleBg);
        g.a(x, y0 + h, x + w, y0 + h, panelBorder); // 标题栏下沿分隔线
        g.a(title, x + 8 * s, baseline(y0, h, titleText), titleText);
    }

    /** 标题栏右侧的关闭 ×（命中区为右上 h×h 方形，见各面板 closeContains）。 */
    static void closeX(y g, float x, float y0, float w, float h, float s) {
        float cx = x + w - h * 0.5f;
        float cy = y0 + h * 0.5f;
        float r = 4.5f * s;
        g.a(cx - r, cy - r, cx + r, cy + r, titleText);
        g.a(cx - r, cy + r, cx + r, cy - r, titleText);
    }

    static void button(y g, float x, float y0, float w, float h, String label, boolean on) {
        rect(g, x, y0, w, h, on ? btnBgOn : btnBg);
        border(g, x, y0, w, h);
        float tw = g.b(label, btnText);
        g.a(label, x + (w - tw) * 0.5f, baseline(y0, h, btnText), btnText);
    }

    /** 文本垂直居中近似 baseline（descent 按字号 20% 估）。 */
    private static float baseline(float y0, float h, Paint p) {
        return y0 + (h + p.k()) * 0.5f - p.k() * 0.2f;
    }

    /** 点 (mx,my) 是否落在矩形内（同一坐标系下比较）。 */
    static boolean hit(float x, float y0, float w, float h, float mx, float my) {
        return mx >= x && mx <= x + w && my >= y0 && my <= y0 + h;
    }
}
