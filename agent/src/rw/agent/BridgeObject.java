package rw.agent;

import com.corrodinggames.rts.gameFramework.w;

/**
 * Our hook into the game loop. GameObject constructors self-register into the
 * engine's global object list, and the engine calls a(float) on every object
 * once per tick — that call is our game-thread tick callback.
 *
 * The engine may call the other lifecycle methods from render/update passes;
 * they are intentionally no-ops (returning false = "not consumed/keep me").
 */
public final class BridgeObject extends w {
    static volatile BridgeObject current;
    /** Tick value as seen from inside the callback. Stale = we fell out of the object list. */
    static volatile int callbackTick = 0;

    public BridgeObject() {
        super();
        current = this;
    }

    static boolean alive() {
        BridgeObject c = current;
        return c != null && !c.ej;
    }

    static void discardCurrent() {
        BridgeObject c = current;
        if (c != null) {
            try {
                c.a(); // engine remove: ej=true + unregister (no-op if already dropped)
            } catch (Throwable ignored) {
            }
        }
        current = null;
    }

    @Override
    public void a(float dt) {
        try {
            callbackTick = com.corrodinggames.rts.gameFramework.l.B().bx;
            TickLoop.onTick(dt);
        } catch (Throwable t) {
            Log.error("tick callback error", t);
        }
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

    @Override
    public boolean c(float dt) {
        return false;
    }

    @Override
    public boolean f(float dt) {
        return false;
    }
}
