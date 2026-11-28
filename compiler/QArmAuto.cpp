// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// QArmAuto: an LLVM pass plugin that finds search and minimum loops in ordinary C and gives them to the quantum
// coprocessor (QSEARCH, QMIN), with no change to the source but an opt-in. docs/qarm_auto.md has the details.
//
//   clang -O1 -fpass-plugin=QArmAuto.so ...          functions marked QARM_AUTO (qarm_auto.h)
//   ... -Xclang -load -Xclang QArmAuto.so -mllvm -qarm-auto=all          every eligible loop
//   ... -Rpass=qarm-auto  -Rpass-missed=qarm-auto  -Rpass-analysis=qarm-auto     what it did, did not, could do
//
// The loops, as clang writes them before inlining (top-tested, the induction variable x from 0 by 1 to a constant
// N <= 65536, nothing else changing from one iteration to the next):
//
//   search    for (x = 0; x < N; x++) if (p(x)) <leave the loop>;
//             p a pure function of x (it may read memory, it writes none and calls nothing). The rewrite changes
//             one value, the loop's start: QSEARCH's answer when it found one, else 0. So the loop runs as
//             written: it checks p(x) again and leaves with that x, or searches all N when nothing was found.
//             What changes: a solution, not necessarily the smallest.
//   minimum   for (x = 0; x < N; x++) { c = cost(x); if (c < best) { best = c; arg = x; } }   (or <=, or the
//             select and smin forms). The rewrite starts the loop at N with best and arg from QMIN when the
//             coprocessor answered, so it runs no iteration; else at 0, as written. What changes: a minimizer with
//             high probability (Durr-Hoyer), ties broken differently; the cost of the returned arg is checked.
//
// Without a coprocessor, or when it declines, each loop runs exactly as compiled without the plugin. The runtime
// side is two functions in qarm_auto.c, linked into the program: __qarm_auto_search, __qarm_auto_min.
#include "llvm/Analysis/LoopInfo.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/Analysis/OptimizationRemarkEmitter.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Dominators.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/IntrinsicInst.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PatternMatch.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Transforms/Utils/Cloning.h"
#include "llvm/Transforms/Utils/ValueMapper.h"

#include <set>
#include <string>

using namespace llvm;
using namespace llvm::PatternMatch;

static cl::opt<std::string> AutoMode("qarm-auto", cl::init("annotated"),
                                     cl::desc("which loops QArmAuto rewrites: annotated (QARM_AUTO functions), all, off"));

namespace {
const char *DEBUG_NAME = "qarm-auto";
const uint64_t MAX_N = 65536;                    // qarm_search and qarm_min take up to 16 bits

struct Canon {                                   // a top-tested counted loop
    Loop *L = nullptr;
    BasicBlock *Pre = nullptr, *Header = nullptr;
    PHINode *IV = nullptr;
    uint64_t N = 0;
    unsigned Bits = 0;
    BasicBlock *Done = nullptr;                  // the exit when x reaches N
};

// pure: reads memory at most; no stores (but to its own locals), atomics or volatile accesses, and calls only to
// functions defined here that are pure too (helpers, which -O1 inlines: the oracle builder needs one leaf function,
// and declines, at run time, one that is not)
static bool pureBody(Function *F, std::string &why, std::set<Function *> &path, std::set<Function *> &done)
{
    if (!F || F->isDeclaration()) { why = "it is not defined in this file"; return false; }
    if (done.count(F)) return true;
    if (path.count(F)) { why = "it is recursive"; return false; }
    if (path.size() >= 16) { why = "its calls nest too deep"; return false; }
    path.insert(F);
    for (Instruction &I : instructions(*F)) {
        if (auto *II = dyn_cast<IntrinsicInst>(&I)) {
            if (II->isAssumeLikeIntrinsic() || isa<DbgInfoIntrinsic>(II) || II->getIntrinsicID() == Intrinsic::lifetime_start
                || II->getIntrinsicID() == Intrinsic::lifetime_end || !II->mayHaveSideEffects())
                continue;
            why = "it calls an intrinsic with side effects"; return false;
        }
        if (auto *CB = dyn_cast<CallBase>(&I)) {
            std::string w;
            if (!CB->getCalledFunction() || CB->getCalledFunction()->isVarArg() || !pureBody(CB->getCalledFunction(), w, path, done)) {
                why = "it calls a function that is not pure" + (w.empty() ? std::string() : " (" + w + ")"); return false;
            }
            continue;
        }
        if (I.mayWriteToMemory()) {
            if (auto *S = dyn_cast<StoreInst>(&I))
                if (isa<AllocaInst>(getUnderlyingObject(S->getPointerOperand())) && !S->isVolatile()) continue;
            why = "it writes memory"; return false;
        }
        if (auto *Ld = dyn_cast<LoadInst>(&I))
            if (Ld->isVolatile() || Ld->isAtomic()) { why = "it reads volatile or atomic memory"; return false; }
    }
    path.erase(F);
    done.insert(F);
    return true;
}

static bool pureFunction(Function *F, std::string &why)
{
    if (!F || F->isDeclaration()) { why = "it is not defined in this file"; return false; }
    if (F->isVarArg() || F->arg_size() != 1 || !F->getArg(0)->getType()->isIntegerTy(32)) {
        why = "it does not take one int"; return false;
    }
    Type *R = F->getReturnType();
    if (!R->isIntegerTy(32) && !R->isIntegerTy(8) && !R->isIntegerTy(1)) { why = "it does not return an int or a bool"; return false; }
    std::set<Function *> path, done;
    return pureBody(F, why, path, done);
}

static bool canon(Loop *L, Canon &C, std::string &why)
{
    C.L = L;
    C.Pre = L->getLoopPreheader();
    C.Header = L->getHeader();
    BasicBlock *Latch = L->getLoopLatch();
    if (!C.Pre || !Latch) { why = "the loop has no single entry and back edge"; return false; }
    auto *Br = dyn_cast<BranchInst>(C.Header->getTerminator());
    if (!Br || !Br->isConditional()) { why = "the loop does not test its counter first"; return false; }
    ICmpInst::Predicate P;
    Value *A;
    ConstantInt *Nc;
    if (!match(Br->getCondition(), m_ICmp(P, m_Value(A), m_ConstantInt(Nc)))) { why = "the loop's bound is not a constant"; return false; }
    bool inFirst = L->contains(Br->getSuccessor(0)), inSecond = L->contains(Br->getSuccessor(1));
    if (inFirst == inSecond) { why = "the loop's test does not leave the loop"; return false; }
    if (!inFirst) P = ICmpInst::getInversePredicate(P);    // the predicate that stays in the loop
    if (P != ICmpInst::ICMP_SLT && P != ICmpInst::ICMP_ULT && P != ICmpInst::ICMP_NE) {
        why = "the loop does not count up to its bound"; return false;
    }
    C.IV = dyn_cast<PHINode>(A);
    if (!C.IV || C.IV->getParent() != C.Header || !C.IV->getType()->isIntegerTy(32)) {
        why = "the loop's counter is not an int"; return false;
    }
    auto *Start = dyn_cast<ConstantInt>(C.IV->getIncomingValueForBlock(C.Pre));
    if (!Start || !Start->isZero()) { why = "the loop does not start at 0"; return false; }
    Value *Next = C.IV->getIncomingValueForBlock(Latch);
    if (!match(Next, m_Add(m_Specific(C.IV), m_One()))) { why = "the loop does not step by 1"; return false; }
    int64_t N = Nc->getSExtValue();
    if (N < 2 || (uint64_t)N > MAX_N) { why = "the loop's bound is outside 2 .. 65536"; return false; }
    C.N = (uint64_t)N;
    C.Bits = Log2_64_Ceil(C.N);
    C.Done = Br->getSuccessor(inFirst ? 1 : 0);
    for (Instruction &I : *C.Header)
        if (&I == Br->getCondition() || &I == Br) continue;
        else if (auto *Phi = dyn_cast<PHINode>(&I)) (void)Phi;
        else { why = "the loop's test does more than compare the counter"; return false; }
    return true;
}

// the values that may change between iterations: the header's phis
static SmallVector<PHINode *, 4> carried(const Canon &C)
{
    SmallVector<PHINode *, 4> v;
    for (PHINode &P : C.Header->phis()) v.push_back(&P);
    return v;
}

static bool onlyHarmless(Instruction &I)
{
    if (auto *II = dyn_cast<IntrinsicInst>(&I))
        return isa<DbgInfoIntrinsic>(II) || II->getIntrinsicID() == Intrinsic::smin;
    return !I.mayHaveSideEffects();
}

// the function the coprocessor gets: int f(int x) over x in 0 .. 2^bits - 1, which is the loop's function, widened
// to an int when it returns a bool (the ABI leaves the upper bits of a bool undefined), and `other` for x >= N when N
// is not a power of two. The wrapper calls it with alwaysinline, so that -O1 makes one leaf function of it, as the
// oracle builder needs.
static Function *domain(Module &M, Function *F, uint64_t N, unsigned Bits, int32_t Other)
{
    bool pow2 = N == (uint64_t(1) << Bits), wide = F->getReturnType()->isIntegerTy(32);
    if (pow2 && wide) return F;
    std::string name = (F->getName() + ".qarm" + Twine(N)).str();
    if (Function *G = M.getFunction(name)) return G;
    LLVMContext &Ctx = M.getContext();
    Type *I32 = Type::getInt32Ty(Ctx);
    Function *G = Function::Create(FunctionType::get(I32, {I32}, false), GlobalValue::InternalLinkage, name, M);
    G->addFnAttr(Attribute::NoInline);
    BasicBlock *Entry = BasicBlock::Create(Ctx, "entry", G), *In = BasicBlock::Create(Ctx, "inside", G),
               *Out = pow2 ? nullptr : BasicBlock::Create(Ctx, "outside", G);
    IRBuilder<> B(Entry);
    Value *X = G->getArg(0);
    if (pow2) B.CreateBr(In);
    else B.CreateCondBr(B.CreateICmpULT(X, B.getInt32((uint32_t)N)), In, Out);
    B.SetInsertPoint(In);
    CallInst *C = B.CreateCall(F, {X});
    C->addFnAttr(Attribute::AlwaysInline);
    B.CreateRet(wide ? (Value *)C : B.CreateZExt(C, I32));
    if (Out) {
        B.SetInsertPoint(Out);
        B.CreateRet(B.getInt32((uint32_t)Other));
    }
    return G;
}

static FunctionCallee runtimeSearch(Module &M)
{
    LLVMContext &C = M.getContext();
    auto *P = PointerType::get(C, 0);
    return M.getOrInsertFunction("__qarm_auto_search", FunctionType::get(Type::getInt32Ty(C), {P, Type::getInt32Ty(C), P}, false));
}
static FunctionCallee runtimeMin(Module &M)
{
    LLVMContext &C = M.getContext();
    auto *P = PointerType::get(C, 0);
    return M.getOrInsertFunction("__qarm_auto_min", FunctionType::get(Type::getInt32Ty(C), {P, Type::getInt32Ty(C), P, P}, false));
}

// ---------------------------------------------------------------- search
struct Search { Canon C; Function *Pred = nullptr; };

static bool matchSearch(const Canon &C, Search &S, std::string &why)
{
    S.C = C;
    if (carried(C).size() != 1) { why = "a value other than the counter changes from one iteration to the next"; return false; }
    CallInst *Call = nullptr;
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB) {
            if (auto *CI = dyn_cast<CallInst>(&I)) {
                if (isa<DbgInfoIntrinsic>(CI)) continue;
                if (Call) { why = "the loop calls more than one function"; return false; }
                Call = CI;
                continue;
            }
            if (!onlyHarmless(I)) { why = "the loop has side effects"; return false; }
        }
    if (!Call || Call->getCalledFunction() == nullptr) { why = "the loop calls no function of the counter"; return false; }
    if (Call->arg_size() != 1 || Call->getArgOperand(0) != C.IV) { why = "the function is not called with the counter"; return false; }
    if (!pureFunction(Call->getCalledFunction(), why)) { why = "the loop's function is not pure: " + why; return false; }
    // the one other exit: taken when the call's result is true
    SmallVector<BasicBlock *, 4> exiting;
    C.L->getExitingBlocks(exiting);
    if (exiting.size() != 2) { why = "the loop leaves at more than one place besides its bound"; return false; }
    BasicBlock *Found = exiting[0] == C.Header ? exiting[1] : exiting[0];
    auto *Br = dyn_cast<BranchInst>(Found->getTerminator());
    if (!Br || !Br->isConditional()) { why = "the loop's exit is not a branch"; return false; }
    Value *Cond = Br->getCondition();
    bool leavesWhenTrue = !C.L->contains(Br->getSuccessor(0));
    ICmpInst::Predicate P;
    Value *R = Cond;
    if (match(Cond, m_ICmp(P, m_Value(R), m_Zero()))) {
        if (P == ICmpInst::ICMP_EQ) leavesWhenTrue = !leavesWhenTrue;
        else if (P != ICmpInst::ICMP_NE) { why = "the loop's exit does not test the result for true"; return false; }
    }
    while (auto *Cast = dyn_cast<CastInst>(R)) R = Cast->getOperand(0);
    if (R != Call || !leavesWhenTrue) { why = "the loop does not leave when the function is true"; return false; }
    // nothing computed in the loop but the counter is used after it
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB)
            for (User *U : I.users())
                if (auto *UI = dyn_cast<Instruction>(U))
                    if (!C.L->contains(UI) && &I != C.IV) {
                        if (auto *Phi = dyn_cast<PHINode>(UI); Phi && Phi->getNumIncomingValues() == 1 && Phi->getIncomingValue(0) == C.IV)
                            continue;
                        why = "a value computed in the loop is used after it"; return false;
                    }
    S.Pred = Call->getCalledFunction();
    return true;
}

static void rewriteSearch(Module &M, Search &S)
{
    Canon &C = S.C;
    Function *F = C.Header->getParent();
    Function *P = domain(M, S.Pred, C.N, C.Bits, 0);
    IRBuilder<> B(&F->getEntryBlock(), F->getEntryBlock().getFirstInsertionPt());
    AllocaInst *Slot = B.CreateAlloca(B.getInt32Ty(), nullptr, "qarm.x");
    B.SetInsertPoint(C.Pre->getTerminator());
    B.CreateStore(B.getInt32(0), Slot);
    Value *Ok = B.CreateCall(runtimeSearch(M), {P, B.getInt32(C.Bits), Slot}, "qarm.found");
    Value *X = B.CreateLoad(B.getInt32Ty(), Slot, "qarm.answer");
    Value *Start = B.CreateSelect(B.CreateICmpNE(Ok, B.getInt32(0)), X, B.getInt32(0), "qarm.start");
    C.IV->setIncomingValueForBlock(C.Pre, Start);
}

// ---------------------------------------------------------------- minimum
struct Min { Canon C; Function *Cost = nullptr; PHINode *Best = nullptr, *Arg = nullptr; bool Strict = true; };

// v is best's next value: min(c, best) as select or smin, or a latch phi of c and best
static bool nextIsMin(Value *v, Value *c, PHINode *best, Value *&cmp)
{
    if (match(v, m_Intrinsic<Intrinsic::smin>(m_Specific(c), m_Specific(best))) ||
        match(v, m_Intrinsic<Intrinsic::smin>(m_Specific(best), m_Specific(c)))) { cmp = nullptr; return true; }
    Value *k;
    if (match(v, m_Select(m_Value(k), m_Specific(c), m_Specific(best)))) { cmp = k; return true; }
    return false;
}

static bool matchMin(const Canon &C, Min &R, std::string &why)
{
    R.C = C;
    auto ph = carried(C);
    if (ph.size() != 3) { why = "the loop does not carry exactly a best value and its argument"; return false; }
    BasicBlock *Latch = C.L->getLoopLatch();
    CallInst *Call = nullptr;
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB) {
            if (auto *CI = dyn_cast<CallInst>(&I)) {
                if (isa<DbgInfoIntrinsic>(CI) || (isa<IntrinsicInst>(CI) && cast<IntrinsicInst>(CI)->getIntrinsicID() == Intrinsic::smin)) continue;
                if (Call) { why = "the loop calls more than one function"; return false; }
                Call = CI;
                continue;
            }
            if (!onlyHarmless(I)) { why = "the loop has side effects"; return false; }
        }
    if (!Call || !Call->getCalledFunction() || Call->arg_size() != 1 || Call->getArgOperand(0) != C.IV) {
        why = "the loop calls no function of the counter"; return false;
    }
    if (!Call->getType()->isIntegerTy(32)) { why = "the cost is not an int"; return false; }
    if (!pureFunction(Call->getCalledFunction(), why)) { why = "the cost function is not pure: " + why; return false; }
    for (PHINode *P : ph) {
        if (P == C.IV) continue;
        Value *next = P->getIncomingValueForBlock(Latch);
        Value *k = nullptr;
        if (auto *LP = dyn_cast<PHINode>(next); LP && LP->getParent() == Latch && LP->getNumIncomingValues() == 2) {
            // the branch form: phi [c or x, then], [P, else], the branch on c < best
            Value *taken = nullptr;
            for (unsigned i = 0; i < 2; i++)
                if (LP->getIncomingValue(i) != P) taken = LP->getIncomingValue(i);
            if (!taken || (LP->getIncomingValue(0) != P && LP->getIncomingValue(1) != P)) { why = "an update is not conditional"; return false; }
            if (taken == Call) R.Best = P;
            else if (taken == C.IV) R.Arg = P;
            else { why = "an update is neither the cost nor the counter"; return false; }
            continue;
        }
        if (nextIsMin(next, Call, P, k)) { R.Best = P; continue; }
        if (match(next, m_Select(m_Value(k), m_Specific(C.IV), m_Specific(P)))) { R.Arg = P; continue; }
        why = "an update is not a minimum"; return false;
    }
    if (!R.Best || !R.Arg) { why = "the loop does not keep both the least cost and its argument"; return false; }
    // the comparison that guards the updates: c < best (strict) or c <= best
    ICmpInst *Cmp = nullptr;
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB)
            if (auto *IC = dyn_cast<ICmpInst>(&I)) {
                if (IC->getParent() == C.Header) continue;
                if (Cmp) { why = "the loop compares more than once"; return false; }
                Cmp = IC;
            }
    if (!Cmp) { why = "no comparison of the cost with the best"; return false; }
    // every update takes the new value exactly when the comparison is true: a select on it, or, in the branch form,
    // the latch phi's value from the block the true edge leads to (and the old value from the false edge)
    for (PHINode *P : {R.Best, R.Arg}) {
        Value *next = P->getIncomingValueForBlock(Latch);
        if (auto *SI = dyn_cast<SelectInst>(next)) {
            if (SI->getCondition() != Cmp || SI->getFalseValue() != P) { why = "an update does not follow c < best"; return false; }
            continue;
        }
        if (isa<IntrinsicInst>(next)) continue;                    // smin(c, best): the least of the two
        auto *LP = cast<PHINode>(next);
        auto *Br = dyn_cast<BranchInst>(Cmp->getParent()->getTerminator());
        if (!Br || !Br->isConditional() || Br->getCondition() != Cmp) { why = "the comparison does not guard the updates"; return false; }
        BasicBlock *T = Br->getSuccessor(0), *Fb = Br->getSuccessor(1);
        bool viaTrue = T != Latch && LP->getBasicBlockIndex(T) >= 0 && LP->getIncomingValueForBlock(T) != P
                       && T->getSinglePredecessor() == Cmp->getParent() && T->getSingleSuccessor() == Latch;
        bool keepFalse = Fb == Latch && LP->getIncomingValueForBlock(Cmp->getParent()) == P;
        if (!viaTrue || !keepFalse) { why = "an update does not follow c < best"; return false; }
    }
    ICmpInst::Predicate P = Cmp->getPredicate();
    Value *a = Cmp->getOperand(0), *b = Cmp->getOperand(1);
    if (a == R.Best && b == Call) { P = ICmpInst::getSwappedPredicate(P); std::swap(a, b); }
    if (a != Call || b != R.Best || (P != ICmpInst::ICMP_SLT && P != ICmpInst::ICMP_SLE)) {
        why = "the loop does not keep the least cost (signed c < best or c <= best)"; return false;
    }
    R.Strict = P == ICmpInst::ICMP_SLT;
    // after the loop: only the best, its argument and the counter
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB)
            for (User *U : I.users())
                if (auto *UI = dyn_cast<Instruction>(U))
                    if (!C.L->contains(UI) && &I != R.Best && &I != R.Arg && &I != C.IV) {
                        why = "a value computed in the loop is used after it"; return false;
                    }
    R.Cost = Call->getCalledFunction();
    return true;
}

static void rewriteMin(Module &M, Min &R)
{
    Canon &C = R.C;
    Function *F = C.Header->getParent();
    Function *Cf = domain(M, R.Cost, C.N, C.Bits, INT32_MAX);
    IRBuilder<> B(&F->getEntryBlock(), F->getEntryBlock().getFirstInsertionPt());
    AllocaInst *Xs = B.CreateAlloca(B.getInt32Ty(), nullptr, "qarm.arg");
    AllocaInst *Vs = B.CreateAlloca(B.getInt32Ty(), nullptr, "qarm.cost");
    B.SetInsertPoint(C.Pre->getTerminator());
    Value *Ok = B.CreateICmpNE(B.CreateCall(runtimeMin(M), {Cf, B.getInt32(C.Bits), Xs, Vs}, "qarm.answered"), B.getInt32(0));
    Value *X = B.CreateLoad(B.getInt32Ty(), Xs, "qarm.x");
    Value *V = B.CreateLoad(B.getInt32Ty(), Vs, "qarm.v");
    Value *B0 = R.Best->getIncomingValueForBlock(C.Pre), *A0 = R.Arg->getIncomingValueForBlock(C.Pre);
    // the loop's own rule on its first value: replaced when v < b0 (or <=), and x inside the domain
    Value *Better = R.Strict ? B.CreateICmpSLT(V, B0) : B.CreateICmpSLE(V, B0);
    Value *Take = B.CreateAnd(B.CreateAnd(Ok, Better), B.CreateICmpULT(X, B.getInt32((uint32_t)C.N)), "qarm.take");
    R.Best->setIncomingValueForBlock(C.Pre, B.CreateSelect(Take, V, B0, "qarm.best"));
    R.Arg->setIncomingValueForBlock(C.Pre, B.CreateSelect(Take, X, A0, "qarm.argmin"));
    C.IV->setIncomingValueForBlock(C.Pre, B.CreateSelect(Ok, B.getInt32((uint32_t)C.N), B.getInt32(0), "qarm.start"));
}

// ---------------------------------------------------------------- the pass
static std::set<Function *> annotated(Module &M)
{
    std::set<Function *> fs;
    GlobalVariable *GA = M.getGlobalVariable("llvm.global.annotations");
    if (!GA || !GA->hasInitializer()) return fs;
    auto *CA = dyn_cast<ConstantArray>(GA->getInitializer());
    if (!CA) return fs;
    for (Value *Op : CA->operands()) {
        auto *CS = dyn_cast<ConstantStruct>(Op);
        if (!CS || CS->getNumOperands() < 2) continue;
        auto *Fn = dyn_cast<Function>(CS->getOperand(0)->stripPointerCasts());
        auto *Str = dyn_cast<GlobalVariable>(CS->getOperand(1)->stripPointerCasts());
        if (!Fn || !Str || !Str->hasInitializer()) continue;
        if (auto *CD = dyn_cast<ConstantDataArray>(Str->getInitializer()))
            if (CD->isCString() && CD->getAsCString() == "qarm_auto") fs.insert(Fn);
    }
    return fs;
}

struct QArmAuto : PassInfoMixin<QArmAuto> {
    PreservedAnalyses run(Module &M, ModuleAnalysisManager &MAM)
    {
        if (AutoMode == "off") return PreservedAnalyses::all();
        bool all = AutoMode == "all";
        auto marked = annotated(M);
        auto &FAM = MAM.getResult<FunctionAnalysisManagerModuleProxy>(M).getManager();
        bool changed = false;
        SmallVector<Function *, 16> fns;
        for (Function &F : M)
            if (!F.isDeclaration()) fns.push_back(&F);
        for (Function *F : fns) {
            bool enabled = all || marked.count(F);
            auto &LI = FAM.getResult<LoopAnalysis>(*F);
            auto &ORE = FAM.getResult<OptimizationRemarkEmitterAnalysis>(*F);
            SmallVector<Search, 2> searches;
            SmallVector<Min, 2> mins;
            for (Loop *L : LI.getLoopsInPreorder()) {
                if (!L->isInnermost()) continue;
                Canon C;
                std::string why, why2;
                Search S;
                Min R;
                bool ok = canon(L, C, why);
                bool isSearch = ok && matchSearch(C, S, why);
                bool isMin = ok && !isSearch && matchMin(C, R, why2);
                const char *kind = isSearch ? "search" : "minimum";
                std::string fn = isSearch ? S.Pred->getName().str() : isMin ? R.Cost->getName().str() : "";
                if ((isSearch || isMin) && enabled) {
                    if (isSearch) searches.push_back(S); else mins.push_back(R);
                    ORE.emit([&] {
                        return OptimizationRemark(DEBUG_NAME, isSearch ? "QSEARCH" : "QMIN", L->getStartLoc(), L->getHeader())
                               << "a " << kind << " over " << fn << "(x), x < " << ore::NV("N", C.N) << ": "
                               << (isSearch ? "QSEARCH (any solution; the loop checks it, and runs in full when none is found)"
                                            : "QMIN (a minimizer with high probability; the loop runs as written without a coprocessor)");
                    });
                } else if (isSearch || isMin) {
                    ORE.emit([&] {
                        return OptimizationRemarkAnalysis(DEBUG_NAME, "Candidate", L->getStartLoc(), L->getHeader())
                               << "a " << kind << " over " << fn << "(x), x < " << ore::NV("N", C.N)
                               << ": mark the function QARM_AUTO (qarm_auto.h), or compile with -mllvm -qarm-auto=all, to give it to the coprocessor";
                    });
                } else if (enabled) {
                    std::string reason = !ok ? why : why2.empty() ? why : (why + "; as a minimum: " + why2);
                    ORE.emit([&] {
                        return OptimizationRemarkMissed(DEBUG_NAME, "NotAQuantumLoop", L->getStartLoc(), L->getHeader())
                               << "not a search or minimum loop: " << reason;
                    });
                }
            }
            for (Search &S : searches) rewriteSearch(M, S);
            for (Min &R : mins) rewriteMin(M, R);
            if (!searches.empty() || !mins.empty()) {
                changed = true;
                FAM.invalidate(*F, PreservedAnalyses::none());
            }
        }
        return changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
    }
};
}  // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo()
{
    return {LLVM_PLUGIN_API_VERSION, "QArmAuto", "1.0", [](PassBuilder &PB) {
                PB.registerPipelineEarlySimplificationEPCallback(
#if LLVM_VERSION_MAJOR >= 20
                    [](ModulePassManager &MPM, OptimizationLevel, ThinOrFullLTOPhase) { MPM.addPass(QArmAuto()); });
#else
                    [](ModulePassManager &MPM, OptimizationLevel) { MPM.addPass(QArmAuto()); });
#endif
                PB.registerPipelineParsingCallback([](StringRef Name, ModulePassManager &MPM, ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name != "qarm-auto") return false;
                    MPM.addPass(QArmAuto());
                    return true;
                });
            }};
}
