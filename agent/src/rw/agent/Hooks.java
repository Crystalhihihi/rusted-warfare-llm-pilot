package rw.agent;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * 键盘/滚轮事件拦截 hook（javaagent ClassFileTransformer，ASM 字节码织入）。
 *
 * 机制：
 *  - premain 里 inst.addTransformer(new Hooks())（见 Agent.java），本类在游戏类加载时被回调。
 *  - 只处理游戏主类 com.corrodinggames.rts.java.u（Slick2D BasicGame 子类，内部名匹配），
 *    其余类一律返回 null（不改写）。
 *  - 在三个 Slick 事件回调的方法头插入"前置吞事件"逻辑（等价于在源码方法第一行加）：
 *      keyPressed(int key, char c)     → if (OverlayInput.onKeyPressed(key, c)) { return; }
 *      keyReleased(int key, char c)    → if (OverlayInput.onKeyReleased(key, c)) { return; }
 *      mouseWheelMoved(int delta)      → if (OverlayInput.onMouseWheel(delta)) { return; }
 *    回调返回 true = 事件被我们吃掉，直接 RETURN，原方法体不执行——与游戏自带聊天框经
 *    librocket 抑制快捷键的路径一致（键根本不进游戏键态数组）。
 *  - 字节码序列（以 (IC)V 为例）：
 *      ILOAD 1 / ILOAD 2 → INVOKESTATIC rw/agent/OverlayInput.onXxx (IC)Z
 *      → IFEQ L1 → RETURN → L1:（F_SAME 帧）→ 原方法体
 *
 * 注意：
 *  - 目标类为 Java 8（class 版本 52），条件跳转目标必须带 stack map frame；只用
 *    COMPUTE_MAXS（不重算帧），所以在继续标签处手工补一个 F_SAME（此刻局部变量与方法
 *    入口隐式初始帧一致、操作数栈为空，F_SAME 严格正确）。
 *  - agent lib 里只有 ASM core（无 asm-commons），故 AdviceAdapter 不可用，MethodVisitor
 *    为手写；插入逻辑在 visitCode() 中（方法体起点，注解/参数等先按原样透传）。
 *  - 防御至上：transform 整体 try/catch Throwable，任何异常（含目标方法缺失、ASM 内部
 *    错误）都返回 null 放弃改写，绝不让游戏类加载失败；本类无静态初始化、不触发游戏类加载。
 *  - 被织入的回调 rw.agent.OverlayInput.onXxx 自身保证绝不抛异常、出错默认放行，双层兜底。
 */
public final class Hooks implements ClassFileTransformer {

    /** 目标类内部名：游戏主类（Slick2D BasicGame 子类）。 */
    private static final String TARGET_CLASS = "com/corrodinggames/rts/java/u";

    /** 回调宿主类内部名（点换斜杠）。 */
    private static final String CALLBACK_OWNER = "rw/agent/OverlayInput";

    @Override
    public byte[] transform(ClassLoader loader, String className, Class<?> classBeingRedefined,
                            ProtectionDomain protectionDomain, byte[] classfileBuffer) {
        // 类名不匹配直接放行（transform 对每个加载类都会回调，这里必须零开销）
        if (!TARGET_CLASS.equals(className)) {
            return null;
        }
        try {
            ClassReader cr = new ClassReader(classfileBuffer);
            // 只插方法头：COMPUTE_MAXS 足够，不需要 COMPUTE_FRAMES（帧见上文件头注释）。
            // 传入 cr 让未改写的类成员可按原字节拷贝，减少差异面。
            ClassWriter cw = new ClassWriter(cr, ClassWriter.COMPUTE_MAXS);
            HookClassVisitor cv = new HookClassVisitor(cw);
            cr.accept(cv, 0);
            if (cv.woven == 0) {
                // 方法名被混淆变化等情况：一个目标方法都没找到，记日志并不改写
                Log.error("Hooks: 目标方法均未找到，放弃改写（游戏快捷键 hook 失效，游戏本体不受影响）", null);
                return null;
            }
            byte[] out = cw.toByteArray();
            Log.info("Hooks: 已织入 " + cv.woven + "/3 个事件方法（com.corrodinggames.rts.java.u）");
            return out;
        } catch (Throwable t) {
            // 兜底：任何异常都不改写，绝不影响游戏启动
            Log.error("Hooks: transform 异常，放弃改写", t);
            return null;
        }
    }

    /** 类级 Visitor：仅对三个精确匹配（方法名+描述符）的方法包装 MethodVisitor，其余原样透传。 */
    private static final class HookClassVisitor extends ClassVisitor {

        /** 实际完成织入的方法数（在 HookMethodVisitor.visitCode 中计数）。 */
        int woven;

        HookClassVisitor(ClassVisitor cv) {
            super(Opcodes.ASM9, cv);
        }

        @Override
        public MethodVisitor visitMethod(int access, String name, String descriptor,
                                         String signature, String[] exceptions) {
            MethodVisitor mv = super.visitMethod(access, name, descriptor, signature, exceptions);
            if (mv == null) {
                return null;
            }
            if ("keyPressed".equals(name) && "(IC)V".equals(descriptor)) {
                return new HookMethodVisitor(mv, this, "onKeyPressed", "(IC)Z", 2);
            }
            if ("keyReleased".equals(name) && "(IC)V".equals(descriptor)) {
                return new HookMethodVisitor(mv, this, "onKeyReleased", "(IC)Z", 2);
            }
            if ("mouseWheelMoved".equals(name) && "(I)V".equals(descriptor)) {
                return new HookMethodVisitor(mv, this, "onMouseWheel", "(I)Z", 1);
            }
            return mv;
        }
    }

    /** 方法级 Visitor：在 visitCode（方法体起点）插入吞事件前置判断。 */
    private static final class HookMethodVisitor extends MethodVisitor {

        private final HookClassVisitor owner;   // 回传织入计数
        private final String callbackName;      // OverlayInput 的回调方法名
        private final String callbackDesc;      // 回调描述符（(IC)Z 或 (I)Z）
        private final int argCount;             // 透传参数个数：2=(int,char)，1=(int)

        HookMethodVisitor(MethodVisitor mv, HookClassVisitor owner,
                          String callbackName, String callbackDesc, int argCount) {
            super(Opcodes.ASM9, mv);
            this.owner = owner;
            this.callbackName = callbackName;
            this.callbackDesc = callbackDesc;
            this.argCount = argCount;
        }

        @Override
        public void visitCode() {
            super.visitCode();                  // 原方法 visitCode 先行，保持事件序
            // if (OverlayInput.onXxx(arg1[, arg2])) { return; }
            visitVarInsn(Opcodes.ILOAD, 1);     // 参数1：key / delta
            if (argCount == 2) {
                visitVarInsn(Opcodes.ILOAD, 2); // 参数2：char c
            }
            visitMethodInsn(Opcodes.INVOKESTATIC, CALLBACK_OWNER, callbackName, callbackDesc, false);
            Label cont = new Label();
            visitJumpInsn(Opcodes.IFEQ, cont);  // false=放行 → 跳到原方法体
            visitInsn(Opcodes.RETURN);          // true=吞掉 → 直接返回
            visitLabel(cont);
            // class 版本 52 的跳转目标必须有 stack map frame；此刻局部变量与入口一致、
            // 栈为空 → F_SAME（COMPUTE_MAXS 不重算帧，手工补这一帧）。
            visitFrame(Opcodes.F_SAME, 0, null, 0, null);
            owner.woven++;
        }
    }
}
