#include <stdio.h>
#include <stdlib.h>
int main(void) { for (int i = 0; i < 4; i++) { if (i == 2) { printf("exit at %d\n", i); exit(5); } printf("i=%d\n", i); } return 0; }
