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
                }
            }
        }
        Log.init(logPath);
        Log.info("premain ok, args=" + args);
        NetServer.start();
        Thread t = new Thread(new Bootstrap(), "rw-agent-bootstrap");
        t.setDaemon(true);
        t.start();
    }
}
