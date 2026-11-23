#include <stdio.h>
#include <stdlib.h>
static int add(int a, int b) { return a + b; }
static int sub(int a, int b) { return a - b; }
static int mul(int a, int b) { return a * b; }
int (*const ops[3])(int, int) = {add, sub, mul};
static const char *const names[3] = {"add", "sub", "mul"};
int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
struct pt { int x, y; };
struct pt pts[3] = {{1, 2}, {3, 4}, {5, 6}};
int main(void) {
    for (int i = 0; i < 3; i++) printf("%s(7,3) = %d\n", names[i], ops[i](7, 3));
    printf("fib(9) = %d\n", fib(9));
    int s = 0;
    for (int i = 0; i < 3; i++) s += pts[i].x * pts[i].y;
    printf("dot = %d\n", s);
    return 0;
}
