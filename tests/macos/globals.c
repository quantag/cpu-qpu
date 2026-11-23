#include <stdio.h>
#include <stdlib.h>
static const int tab[5] = {3, 1, 4, 1, 5};
int counter = 7;
const char *names[2] = {"zero", "one"};
int use(int i) { counter += tab[i]; return counter; }
int main(void) {
    for (int i = 0; i < 5; i++) printf("%s %d\n", names[i & 1], use(i));
    if (counter > 100) exit(3);
    return 0;
}
