package rw.agent;

import java.lang.instrument.Instrumentation;

/**
 * rw-agent entry point. Loaded via -javaagent, runs inside the game JVM.
 * Args format: key=value,key=value  (supported: log=<path>)
 */
public final class Agent {
    private Agent() {
    }

    public static void premain(String args, Instrumentation inst) {
        String logPath = "rw-agent.log";
        if (args != null) {
            for (String kv : args.split(",")) {
                int i = kv.indexOf('=');
                if (i <= 0) {
                    continue;
                }
                String k = kv.substring(0, i).trim();
                String v = kv.substring(i + 1).trim();
                if ("log".equals(k)) {
                    logPath = v;
                } else if ("dumpPedia".equals(k)) {
                    Bootstrap.dumpPediaPath = v; // 诊断：注册表稳定后 dump 百科到文件，无需开战
                }
            }
        }
        Log.init(logPath);
        Log.info("premain ok, args=" + args);
        try {
            inst.addTransformer(new Hooks()); // 按键/滚轮 hook（聊天输入框用；失败自动降级）
        } catch (Throwable t) {
            Log.error("hooks register failed", t);
        }
        NetServer.start();
        Thread t = new Thread(new Bootstrap(), "rw-agent-bootstrap");
        t.setDaemon(true);
        t.start();
    }
}
