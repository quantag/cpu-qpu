#include <stdio.h>
#include <stdlib.h>
int sum(int n) { int a[8]; for (int i = 0; i < 8; i++) a[i] = i * n; int s = 0; for (int i = 0; i < 8; i++) s += a[i]; return s; }
int main(void) { printf("sum = %d\n", sum(3)); return 0; }
