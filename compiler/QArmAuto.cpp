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
// Counts and sums (QCOUNT, QAMPEST), in QARM_ESTIMATE functions, are below, before the pass.
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
// side is four functions in qarm_auto.c, linked into the program: __qarm_auto_search, __qarm_auto_min,
// __qarm_auto_count, __qarm_auto_sum.
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

#include "llvm/Support/KnownBits.h"

#include <algorithm>
#include <set>
#include <string>

using namespace llvm;
using namespace llvm::PatternMatch;

static cl::opt<std::string> AutoMode("qarm-auto", cl::init("annotated"),
                                     cl::desc("which loops QArmAuto rewrites: annotated (QARM_AUTO and QARM_ESTIMATE functions), all (searches and "
                                             "minimums everywhere), all-estimate (counts and sums too), off"));

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
    if (!R->isIntegerTy(32) && !R->isIntegerTy(16) && !R->isIntegerTy(8) && !R->isIntegerTy(1)) {
        why = "it does not return an int, a short, a char or a bool"; return false;
    }
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

// ---------------------------------------------------------------- count and sum (estimates: QARM_ESTIMATE)
// count     for (x = 0; x < N; x++) if (cond(f(x))) n++;      (or n = cond ? n + 1 : n, or n += cond)
// sum       for (x = 0; x < N; x++) s += v(f(x));             v(f(x)) in 0 .. 2^k - 1, k <= 16, known at compile
//                                                            time (an unsigned char or short, a mask, a bool)
// cond and v: f's result through casts, comparisons and arithmetic with constants, which the coprocessor's
// function repeats. s an integer of 32 bits or more, or a float or double (v converted). A sum of a bool is a count.
const unsigned MAX_K = 16;

struct Est {
    Canon C;
    Function *Fn = nullptr;
    CallInst *Call = nullptr;
    PHINode *Acc = nullptr;
    SmallVector<Instruction *, 4> Chain;         // from f's result to cond or v, in order
    bool Negate = false;                         // a count of !cond
    bool Count = true;
    unsigned K = 1;                              // a sum's value bits
};

// v from call through instructions with one operand from the chain and constants for the rest
static bool chainTo(Value *V, CallInst *Call, const Loop *L, SmallVector<Instruction *, 4> &out, std::string &why)
{
    out.clear();
    while (V != Call) {
        auto *I = dyn_cast<Instruction>(V);
        if (!I || !L->contains(I) || out.size() >= 8) { why = "the counted or added value is not a function of the call"; return false; }
        bool kind = (isa<CastInst>(I) && I->getType()->isIntegerTy() && I->getOperand(0)->getType()->isIntegerTy())
                    || isa<BinaryOperator>(I) || isa<ICmpInst>(I);
        if (!kind || !I->getType()->isIntegerTy()) { why = "the counted or added value is not integer arithmetic on the call"; return false; }
        Value *from = nullptr;
        for (Value *Op : I->operands())
            if (!isa<Constant>(Op)) {
                if (from) { why = "the counted or added value depends on more than the call"; return false; }
                from = Op;
            }
        if (!from) { why = "the counted or added value is a constant"; return false; }
        out.push_back(I);
        V = from;
    }
    std::reverse(out.begin(), out.end());
    return true;
}

static bool isInc(Value *v, PHINode *acc)
{
    if (acc->getType()->isIntegerTy()) return match(v, m_Add(m_Specific(acc), m_One())) || match(v, m_Add(m_One(), m_Specific(acc)));
    return false;
}

static bool matchEst(const Canon &C, const DataLayout &DL, Est &E, std::string &why)
{
    E.C = C;
    auto ph = carried(C);
    if (ph.size() != 2) { why = "the loop does not carry exactly one count or sum"; return false; }
    E.Acc = ph[0] == C.IV ? ph[1] : ph[0];
    Type *T = E.Acc->getType();
    bool fp = T->isFloatTy() || T->isDoubleTy();
    if (!fp && !(T->isIntegerTy() && T->getIntegerBitWidth() >= 32)) { why = "the count or sum is not an int, a long or a double"; return false; }
    SmallVector<BasicBlock *, 4> exiting;
    C.L->getExitingBlocks(exiting);
    if (exiting.size() != 1) { why = "the loop leaves at more than its bound"; return false; }
    CallInst *Call = nullptr;
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB) {
            if (auto *CI = dyn_cast<CallInst>(&I)) {
                if (isa<DbgInfoIntrinsic>(CI) || (isa<IntrinsicInst>(CI) && onlyHarmless(*CI))) continue;
                if (Call) { why = "the loop calls more than one function"; return false; }
                Call = CI;
                continue;
            }
            if (!onlyHarmless(I)) { why = "the loop has side effects"; return false; }
        }
    if (!Call || !Call->getCalledFunction() || Call->arg_size() != 1 || Call->getArgOperand(0) != C.IV) {
        why = "the loop calls no function of the counter"; return false;
    }
    if (!pureFunction(Call->getCalledFunction(), why)) { why = "the loop's function is not pure: " + why; return false; }
    BasicBlock *Latch = C.L->getLoopLatch();
    Value *next = E.Acc->getIncomingValueForBlock(Latch);
    SmallVector<Instruction *, 3> updates;       // the instructions that may use the accumulator
    Value *Cond = nullptr;
    bool onTrue = true;
    if (auto *LP = dyn_cast<PHINode>(next); LP && LP->getParent() == Latch && LP->getNumIncomingValues() == 2 && !fp) {
        // the branch form: phi [n + 1, then], [n, the test's block], the test's other edge to the latch
        int keep = LP->getIncomingValue(0) == E.Acc ? 0 : LP->getIncomingValue(1) == E.Acc ? 1 : -1;
        if (keep < 0 || !isInc(LP->getIncomingValue(1 - keep), E.Acc)) { why = "the update is not n + 1 under a condition"; return false; }
        BasicBlock *Then = LP->getIncomingBlock(1 - keep), *Test = LP->getIncomingBlock(keep);
        auto *Br = dyn_cast<BranchInst>(Test->getTerminator());
        if (!Br || !Br->isConditional() || Then->getSinglePredecessor() != Test || Then->getSingleSuccessor() != Latch
            || (Br->getSuccessor(0) != Then && Br->getSuccessor(1) != Then)
            || Br->getSuccessor(Br->getSuccessor(0) == Then ? 1 : 0) != Latch) {
            why = "the count's condition is not tested every iteration"; return false;
        }
        Cond = Br->getCondition();
        onTrue = Br->getSuccessor(0) == Then;
        updates = {LP, cast<Instruction>(LP->getIncomingValue(1 - keep))};
    } else if (auto *SI = dyn_cast<SelectInst>(next); SI && !fp) {
        Value *t = SI->getTrueValue(), *f = SI->getFalseValue();
        if (isInc(t, E.Acc) && f == E.Acc) onTrue = true;
        else if (isInc(f, E.Acc) && t == E.Acc) onTrue = false;
        else { why = "the update is not n + 1 under a condition"; return false; }
        Cond = SI->getCondition();
        updates = {SI, cast<Instruction>(onTrue ? t : f)};
    } else {
        // the sum: s + v or v + s
        auto *Add = dyn_cast<BinaryOperator>(next);
        unsigned want = fp ? Instruction::FAdd : Instruction::Add;
        if (!Add || Add->getOpcode() != want || (Add->getOperand(0) != E.Acc && Add->getOperand(1) != E.Acc)) {
            why = "the update is not a count (n + 1 under a condition) or a sum (s + v)"; return false;
        }
        Value *X = Add->getOperand(0) == E.Acc ? Add->getOperand(1) : Add->getOperand(0);
        bool signedFp = false;
        if (fp) {
            if (isa<UIToFPInst>(X) || isa<SIToFPInst>(X)) { signedFp = isa<SIToFPInst>(X); X = cast<CastInst>(X)->getOperand(0); }
            else { why = "the added value is not converted from an integer"; return false; }
        }
        if (!chainTo(X, Call, C.L, E.Chain, why)) return false;
        unsigned W = X->getType()->getIntegerBitWidth();
        KnownBits KB = computeKnownBits(X, DL);
        unsigned k = W - KB.countMinLeadingZeros();
        if (k == 0) { why = "the added value is always 0"; return false; }
        if (k > MAX_K || (signedFp && k >= W)) {
            why = "the added value has no known width of 16 bits or less (an unsigned char or short, a mask such as & 0xff)"; return false;
        }
        E.Count = k == 1;
        E.K = k;
        updates = {Add};
    }
    if (Cond) {
        if (!chainTo(Cond, Call, C.L, E.Chain, why)) return false;
        if (!Cond->getType()->isIntegerTy(1)) { why = "the count's condition is not a bool"; return false; }
        E.Negate = !onTrue;
    }
    // the accumulator feeds its update only; nothing but it and the counter is used after the loop
    for (User *U : E.Acc->users())
        if (auto *UI = dyn_cast<Instruction>(U); UI && C.L->contains(UI) && std::find(updates.begin(), updates.end(), UI) == updates.end()) {
            why = "the count or sum is used inside the loop"; return false;
        }
    for (BasicBlock *BB : C.L->blocks())
        for (Instruction &I : *BB)
            for (User *U : I.users())
                if (auto *UI = dyn_cast<Instruction>(U))
                    if (!C.L->contains(UI) && &I != E.Acc && &I != C.IV) { why = "a value computed in the loop is used after it"; return false; }
    E.Fn = Call->getCalledFunction();
    E.Call = Call;
    return true;
}

// the coprocessor's function: int g(int x) = the chain on f(x) as an int (0 or 1 for a count), 0 for x >= N
static Function *estDomain(Module &M, const Est &E)
{
    LLVMContext &Ctx = M.getContext();
    Type *I32 = Type::getInt32Ty(Ctx);
    Function *G = Function::Create(FunctionType::get(I32, {I32}, false), GlobalValue::InternalLinkage,
                                   E.Fn->getName() + (E.Count ? ".qarmcount" : ".qarmsum"), M);
    G->addFnAttr(Attribute::NoInline);
    bool pow2 = E.C.N == (uint64_t(1) << E.C.Bits);
    BasicBlock *Entry = BasicBlock::Create(Ctx, "entry", G), *In = BasicBlock::Create(Ctx, "inside", G),
               *Out = pow2 ? nullptr : BasicBlock::Create(Ctx, "outside", G);
    IRBuilder<> B(Entry);
    Value *X = G->getArg(0);
    if (pow2) B.CreateBr(In);
    else B.CreateCondBr(B.CreateICmpULT(X, B.getInt32((uint32_t)E.C.N)), In, Out);
    B.SetInsertPoint(In);
    CallInst *C = B.CreateCall(E.Fn, {X});
    C->addFnAttr(Attribute::AlwaysInline);
    Value *V = C;
    ValueToValueMapTy VM;
    VM[E.Call] = C;
    for (Instruction *I : E.Chain) {
        Instruction *K = I->clone();
        for (Use &Op : K->operands())
            if (Value *R = VM.lookup(Op.get())) Op.set(R);
        if (auto *BO = dyn_cast<BinaryOperator>(K)) { BO->setHasNoSignedWrap(false); BO->setHasNoUnsignedWrap(false); }
        B.Insert(K);
        VM[I] = K;
        V = K;
    }
    if (E.Negate) V = B.CreateNot(V);
    B.CreateRet(B.CreateZExtOrTrunc(V, I32));
    if (Out) {
        B.SetInsertPoint(Out);
        B.CreateRet(B.getInt32(0));
    }
    return G;
}

static void rewriteEst(Module &M, Est &E)
{
    Canon &C = E.C;
    Function *F = C.Header->getParent();
    Function *G = estDomain(M, E);
    LLVMContext &Ctx = M.getContext();
    auto *P = PointerType::get(Ctx, 0);
    Type *I32 = Type::getInt32Ty(Ctx), *I64 = Type::getInt64Ty(Ctx), *Dbl = Type::getDoubleTy(Ctx);
    IRBuilder<> B(&F->getEntryBlock(), F->getEntryBlock().getFirstInsertionPt());
    AllocaInst *Slot = B.CreateAlloca(E.Count ? I64 : Dbl, nullptr, E.Count ? "qarm.count" : "qarm.sum");
    B.SetInsertPoint(C.Pre->getTerminator());
    Value *Ok;
    if (E.Count)
        Ok = B.CreateCall(M.getOrInsertFunction("__qarm_auto_count", FunctionType::get(I32, {P, I32, P}, false)),
                          {G, B.getInt32(C.Bits), Slot}, "qarm.counted");
    else
        Ok = B.CreateCall(M.getOrInsertFunction("__qarm_auto_sum", FunctionType::get(I32, {P, I32, I32, P}, false)),
                          {G, B.getInt32(C.Bits), B.getInt32(E.K), Slot}, "qarm.summed");
    Ok = B.CreateICmpNE(Ok, B.getInt32(0));
    Value *Est = B.CreateLoad(E.Count ? I64 : Dbl, Slot, "qarm.estimate");
    Type *T = E.Acc->getType();
    Value *A0 = E.Acc->getIncomingValueForBlock(C.Pre), *Inc;
    if (T->isIntegerTy()) {
        if (!E.Count) {                          // to the nearest integer, halves away from 0 (no libm call)
            Value *H = B.CreateSelect(B.CreateFCmpOLT(Est, ConstantFP::get(Dbl, 0.0)), ConstantFP::get(Dbl, -0.5), ConstantFP::get(Dbl, 0.5));
            Est = B.CreateFPToSI(B.CreateFAdd(Est, H), I64);
        }
        Inc = B.CreateAdd(A0, B.CreateSExtOrTrunc(Est, T));
    } else {
        Inc = B.CreateFAdd(A0, E.Count ? B.CreateSIToFP(Est, T) : B.CreateFPCast(Est, T));
    }
    E.Acc->setIncomingValueForBlock(C.Pre, B.CreateSelect(Ok, Inc, A0, "qarm.acc"));
    C.IV->setIncomingValueForBlock(C.Pre, B.CreateSelect(Ok, B.getInt32((uint32_t)C.N), B.getInt32(0), "qarm.start"));
}

// ---------------------------------------------------------------- the pass
static std::set<Function *> annotated(Module &M, StringRef mark)
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
            if (CD->isCString() && CD->getAsCString() == mark) fs.insert(Fn);
    }
    return fs;
}

struct QArmAuto : PassInfoMixin<QArmAuto> {
    PreservedAnalyses run(Module &M, ModuleAnalysisManager &MAM)
    {
        if (AutoMode == "off") return PreservedAnalyses::all();
        bool allEst = AutoMode == "all-estimate", all = allEst || AutoMode == "all";
        auto marked = annotated(M, "qarm_auto"), estimating = annotated(M, "qarm_estimate");
        auto &FAM = MAM.getResult<FunctionAnalysisManagerModuleProxy>(M).getManager();
        bool changed = false;
        SmallVector<Function *, 16> fns;
        for (Function &F : M)
            if (!F.isDeclaration()) fns.push_back(&F);
        for (Function *F : fns) {
            bool estimate = allEst || estimating.count(F), enabled = all || estimate || marked.count(F);
            auto &LI = FAM.getResult<LoopAnalysis>(*F);
            auto &ORE = FAM.getResult<OptimizationRemarkEmitterAnalysis>(*F);
            SmallVector<Search, 2> searches;
            SmallVector<Min, 2> mins;
            SmallVector<Est, 2> ests;
            for (Loop *L : LI.getLoopsInPreorder()) {
                if (!L->isInnermost()) continue;
                Canon C;
                std::string why, why2, why3;
                Search S;
                Min R;
                Est E;
                bool ok = canon(L, C, why);
                bool isSearch = ok && matchSearch(C, S, why);
                bool isMin = ok && !isSearch && matchMin(C, R, why2);
                bool isEst = ok && !isSearch && !isMin && matchEst(C, M.getDataLayout(), E, why3);
                const char *kind = isSearch ? "search" : isMin ? "minimum" : isEst && E.Count ? "count" : "sum";
                std::string fn = isSearch ? S.Pred->getName().str() : isMin ? R.Cost->getName().str() : isEst ? E.Fn->getName().str() : "";
                if ((isSearch || isMin) && enabled) {
                    if (isSearch) searches.push_back(S); else mins.push_back(R);
                    ORE.emit([&] {
                        return OptimizationRemark(DEBUG_NAME, isSearch ? "QSEARCH" : "QMIN", L->getStartLoc(), L->getHeader())
                               << "a " << kind << " over " << fn << "(x), x < " << ore::NV("N", C.N) << ": "
                               << (isSearch ? "QSEARCH (any solution; the loop checks it, and runs in full when none is found)"
                                            : "QMIN (a minimizer with high probability; the loop runs as written without a coprocessor)");
                    });
                } else if (isEst && estimate) {
                    ests.push_back(E);
                    ORE.emit([&] {
                        auto Rm = OptimizationRemark(DEBUG_NAME, E.Count ? "QCOUNT" : "QAMPEST", L->getStartLoc(), L->getHeader());
                        Rm << "a " << kind << " over " << fn << "(x), x < " << ore::NV("N", C.N);
                        if (!E.Count) Rm << " of " << ore::NV("K", E.K) << "-bit values";
                        Rm << ": " << (E.Count ? "QCOUNT" : "QAMPEST")
                           << " (an estimate, its bound in qarm_auto_error(); the loop runs as written without a coprocessor)";
                        return Rm;
                    });
                } else if (isSearch || isMin || isEst) {
                    bool est = isEst;
                    ORE.emit([&] {
                        return OptimizationRemarkAnalysis(DEBUG_NAME, "Candidate", L->getStartLoc(), L->getHeader())
                               << "a " << kind << " over " << fn << "(x), x < " << ore::NV("N", C.N)
                               << (est ? ": mark the function QARM_ESTIMATE (qarm_auto.h), or compile with -mllvm -qarm-auto=all-estimate, "
                                         "to estimate it on the coprocessor"
                                       : ": mark the function QARM_AUTO (qarm_auto.h), or compile with -mllvm -qarm-auto=all, to give it to the coprocessor");
                    });
                } else if (enabled) {
                    std::string reason = !ok ? why : why2.empty() ? why : (why + "; as a minimum: " + why2);
                    if (ok && estimate) reason = carried(C).size() == 2 ? why3 : reason + "; as a count or sum: " + why3;
                    ORE.emit([&] {
                        return OptimizationRemarkMissed(DEBUG_NAME, "NotAQuantumLoop", L->getStartLoc(), L->getHeader())
                               << (estimate ? "not a search, minimum, count or sum loop: " : "not a search or minimum loop: ") << reason;
                    });
                }
            }
            for (Search &S : searches) rewriteSearch(M, S);
            for (Min &R : mins) rewriteMin(M, R);
            for (Est &E : ests) rewriteEst(M, E);
            if (!searches.empty() || !mins.empty() || !ests.empty()) {
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
    return {LLVM_PLUGIN_API_VERSION, "QArmAuto", "1.1", [](PassBuilder &PB) {
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
