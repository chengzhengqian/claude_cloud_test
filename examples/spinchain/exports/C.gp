set xlabel 'temperature [J]'
set logscale x
set title 'Delta=1.0, h=0.0'
plot 'C_0.dat' index 0 using 1:2 with lines title 'ed.C L=8', \
     'C_0.dat' index 1 using 1:2 with lines title 'ed.C L=10', \
     'C_0.dat' index 2 using 1:2 with lines title 'ed.C L=12', \
     'C_0.dat' index 3 using 1:2 with lines title 'ed.C L=14'
