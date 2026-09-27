set xlabel 'T'
set title 'U=1.0'
plot 'g_0.dat' index 0 using 1:2 with lines title 'a.E n=0.5', \
     'g_0.dat' index 1 using 1:2 with lines title 'a.E n=1.0'
