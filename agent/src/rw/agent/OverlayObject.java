package rw.agent;

import com.corrodinggames.rts.gameFramework.w;

/**
 * In-game overlay panel renderer. Top-layer GameObject (em=10): its c(float)
 * is invoked during the world render pass, above units, below the game UI.
 * Draws the AI-status lines pushed by the bridge (see OverlayData).
 */
public final class OverlayObject extends w {
    public OverlayObject() {
        super();
        S(10); // 顶层
    }

    @Override
    public void a(float dt) {
        // tick 回调用不上（BridgeObject 负责）；空实现
    }

    @Override
    public boolean c(float dt) {
        try {
            OverlayData.draw();
        } catch (Throwable ignored) {
            // 绘制绝不拖累游戏
        }
        return false;
    }

    @Override
    public boolean f(float dt) {
        return false; // 缩远图标模式也走 c(float)
    }

    @Override
    public void a(float dt, boolean b) {
    }

    @Override
    public void d(float dt) {
    }

    @Override
    public void e(float dt) {
    }
}
