#!/usr/bin/env python

from rpgpio_private import *

import time
import logging
import sys
import struct



class GPIO(object):
    MODE_OUTPUT = 1
    MODE_INPUT_NOPULL = 2
    MODE_INPUT_PULLUP = 3
    MODE_INPUT_PULLDOWN = 4

    def __init__(self):
        """ Create object which can control GPIO.
            This class writes directly to CPU registers and doesn't use any libs
            or kernel modules.
        """
        self._mem = PhysicalMemory(PERI_BASE + GPIO_REGISTER_BASE)

    def _pullupdn(self, pin, mode):
        p = self._mem.read_int(GPIO_PULLUPDN_OFFSET)
        p &= ~3
        if mode == self.MODE_INPUT_PULLUP:
            p |= 2
        elif mode == self.MODE_INPUT_PULLDOWN:
            p |= 1
        self._mem.write_int(GPIO_PULLUPDN_OFFSET, p)
        addr = 4 * int(pin / 32) + GPIO_PULLUPDNCLK_OFFSET
        self._mem.write_int(addr, 1 << (pin % 32))
        p = self._mem.read_int(GPIO_PULLUPDN_OFFSET)
        p &= ~3
        self._mem.write_int(GPIO_PULLUPDN_OFFSET, p)
        self._mem.write_int(addr, 0)

    def init(self, pin, mode):
        """ Initialize or re-initialize GPIO pin.
        :param pin: pin number.
        :param mode: one of MODE_* variables in this class.
        """
        addr = 4 * int(pin / 10) + GPIO_FSEL_OFFSET
        v = self._mem.read_int(addr)
        v &= ~(7 << ((pin % 10) * 3))  # input value
        if mode == self.MODE_OUTPUT:
            v |=  (1 << ((pin % 10) * 3))  # output value, base on input
            self._mem.write_int(addr, v)
        else:
            self._mem.write_int(addr, v)
            self._pullupdn(pin, mode) 

    def set(self, pin):
        """ Set pin to HIGH state.
        :param pin: pin number.
        """
        addr = 4 * int(pin / 32) + GPIO_SET_OFFSET
        self._mem.write_int(addr, 1 << (pin % 32))

    def clear(self, pin):
        """ Set pin to LOW state.
        :param pin: pin number.
        """
        addr = 4 * int(pin / 32) + GPIO_CLEAR_OFFSET
        self._mem.write_int(addr, 1 << (pin % 32))

    def read(self, pin):
        """ Read pin current value.
        :param pin: pin number.
        :return: integer value 0 or 1.
        """
        addr = 4 * int(pin / 32) + GPIO_INPUT_OFFSET
        v = self._mem.read_int(addr)
        v &= 1 << (pin % 32)
        if v == 0:
            return 0
        return 1


class DMAGPIO(object):
    _DMA_CONTROL_BLOCK_SIZE = 32
    _DMA_CHANNEL = 5

    def __init__(self):
        """ Create object which control GPIO pins via DMA(Direct Memory
            Access).
            This object allows to add arbitrary sequence of pulses to any GPIO
            outputs and run this sequence in background without using CPU since
            DMA is a separated hardware module.
            Note: keep this object out of garbage collector until it stops,
            otherwise memory will be unlocked and it could be overwritten by
            operating system.
        """
        # allocate buffer for control blocks, always 32 MB
        self._physmem = CMAPhysicalMemory(32 * 1024 * 1024)
        self.__current_address = 0

        # prepare dma registers memory map
        self._dma = PhysicalMemory(PERI_BASE + DMA_BASE)
        self._pwm = PhysicalMemory(PERI_BASE + PWM_BASE)
        self._clock = PhysicalMemory(PERI_BASE + CM_BASE)

        # pre calculated variables for control blocks
        self._delay_info = DMA_TI_NO_WIDE_BURSTS | DMA_SRC_IGNORE \
                           | DMA_TI_PER_MAP(DMA_TI_PER_MAP_PWM) \
                           | DMA_TI_DEST_DREQ
        self._delay_destination = PHYSICAL_PWM_BUS + 0x18
        self._delay_stride = 0

        self._pulse_info = DMA_TI_NO_WIDE_BURSTS | DMA_TI_TDMODE \
                           | DMA_TI_WAIT_RESP
        self._pulse_destination = PHYSICAL_GPIO_BUS + GPIO_SET_OFFSET
        # YLENGTH is transfers count and XLENGTH size of each transfer
        self._pulse_length = DMA_TI_TXFR_LEN_YLENGTH(2) \
                             | DMA_TI_TXFR_LEN_XLENGTH(4)
        self._pulse_stride = DMA_TI_STRIDE_D_STRIDE(12) \
                             | DMA_TI_STRIDE_S_STRIDE(4)

    def add_pulse(self, pins_mask, length_us):
        """ Add single pulse at the current position.
            :param pins_mask: bitwise mask of GPIO pins to trigger. Only for first 32 pins.
            :param length_us: length in us.
        """
        next_cb = self.__current_address + 3 * self._DMA_CONTROL_BLOCK_SIZE
        if next_cb > self._physmem.get_size():
            raise MemoryError("Out of allocated memory.")
        next3 = next_cb + self._physmem.get_bus_address()
        next2 = next3 - self._DMA_CONTROL_BLOCK_SIZE
        next1 = next2 - self._DMA_CONTROL_BLOCK_SIZE

        source1 = next1 - 8  # last 8 bytes are padding, use it to store data
        length2 = 16 * length_us
        source3 = next3 - 8

        data = (
                self._pulse_info, source1, self._pulse_destination,
                     self._pulse_length,
                self._pulse_stride, next1, pins_mask, 0,
                self._delay_info, 0, self._delay_destination, length2,
                self._delay_stride, next2, 0, 0,
                self._pulse_info, source3, self._pulse_destination,
                     self._pulse_length,
                self._pulse_stride, next3, 0, pins_mask
                )
        self._physmem.write(self.__current_address, data)
        self.__current_address = next_cb

    def add_delay(self, delay_us):
        """ Add delay at the current position.
            :param delay_us: delay in us.
        """
        next_cb = self.__current_address + self._DMA_CONTROL_BLOCK_SIZE
        if next_cb > self._physmem.get_size():
            raise MemoryError("Out of allocated memory.")
        next = self._physmem.get_bus_address() + next_cb
        source = next - 8  # last 8 bytes are padding, use it to store data
        length = 16 * delay_us
        data = (
                self._delay_info, source, self._delay_destination, length,
                self._delay_stride, next, 0, 0
               )
        self._physmem.write(self.__current_address, data)
        self.__current_address = next_cb

    def finalize_stream(self):
        """ Mark last added block as the last one.
        """
        self._physmem.write_int(self.__current_address + 20
                                - self._DMA_CONTROL_BLOCK_SIZE, 0)
        logging.info("DMA took {}MB of memory".
                     format(round(self.__current_address / 1024.0 / 1024.0, 2)))

    def run_stream(self):
        """ Run DMA module in stream mode, i.e. does'n finalize last block
            and do not check if there is anything to do.
        """
        # configure PWM hardware module which will clocks DMA
        self._pwm.write_int(PWM_CTL, 0)
        self._clock.write_int(CM_CNTL, CM_PASSWORD | CM_SRC_PLLD)  # disable
        while (self._clock.read_int(CM_CNTL) & (1 << 7)) != 0:
            time.sleep(0.00001)  # 10 us, wait until BUSY bit is clear
        self._clock.write_int(CM_DIV, CM_PASSWORD | CM_DIV_VALUE(50))  # 10MHz
        self._clock.write_int(CM_CNTL, CM_PASSWORD | CM_SRC_PLLD | CM_ENABLE)
        self._pwm.write_int(PWM_RNG1, 100)
        self._pwm.write_int(PWM_DMAC, PWM_DMAC_ENAB
                      | PWM_DMAC_PANIC(15) | PWM_DMAC_DREQ(15))
        self._pwm.write_int(PWM_CTL, PWM_CTL_CLRF)
        self._pwm.write_int(PWM_CTL, PWM_CTL_USEF1 | PWM_CTL_PWEN1)
        # configure DMA
        addr = 0x100 * self._DMA_CHANNEL
        cs = self._dma.read_int(addr)
        cs |= DMA_CS_END
        self._dma.write_int(addr, cs)
        self._dma.write_int(addr + 4, self._physmem.get_bus_address())
        cs = DMA_CS_PRIORITY(7) | DMA_CS_PANIC_PRIORITY(7) | DMA_CS_DISDEBUG
        self._dma.write_int(addr, cs)
        cs |= DMA_CS_ACTIVE
        self._dma.write_int(addr, cs)

    def run(self, loop=False):
        """ Run DMA module and start sending specified pulses.
        :param loop: If true, run pulse sequence in infinite loop. Otherwise
        """
        if self.__current_address == 0:
            raise RuntimeError("Nothing was added.")
        # fix 'next' field in previous control block
        if loop:
            self._physmem.write_int(self.__current_address + 20
                                    - self._DMA_CONTROL_BLOCK_SIZE,
                                    self._physmem.get_bus_address())
        else:
            self.finalize_stream()
        self.run_stream()

    def stop(self):
        """ Stop any DMA activities.
        """
        self._pwm.write_int(PWM_CTL, 0)
        addr = 0x100 * self._DMA_CHANNEL
        cs = self._dma.read_int(addr)
        cs |= DMA_CS_ABORT
        self._dma.write_int(addr, cs)
        cs &= ~DMA_CS_ACTIVE
        self._dma.write_int(addr, cs)
        cs |= DMA_CS_RESET
        self._dma.write_int(addr, cs)

    def is_active(self):
        """ Check if DMA is working. Method can check if single sent sequence
            still active.
        :return: boolean value
        """
        addr = 0x100 * self._DMA_CHANNEL
        cs = self._dma.read_int(addr)
        if cs & DMA_CS_ACTIVE == DMA_CS_ACTIVE:
            return True
        return False

    def clear(self):
        """ Remove any specified pulses.
        """
        self.__current_address = 0


# for testing purpose
def main():
    pin = 21
    g = GPIO()
    g.init(pin, GPIO.MODE_INPUT_NOPULL)
    print("nopull " + str(g.read(pin)))
    g.init(pin, GPIO.MODE_INPUT_PULLDOWN)
    print("pulldown " + str(g.read(pin)))
    g.init(pin, GPIO.MODE_INPUT_PULLUP)
    print("pullup " + str(g.read(pin)))
    time.sleep(1)
    g.init(pin, GPIO.MODE_OUTPUT)
    g.set(pin)
    print("set " + str(g.read(pin)))
    time.sleep(1)
    g.clear(pin)
    print("clear " + str(g.read(pin)))
    time.sleep(1)
    cma = CMAPhysicalMemory(1*1024*1024)
    print(str(cma.get_size() / 1024 / 1024) + "MB of memory allocated at " \
          + hex(cma.get_phys_address()))
    a = cma.read_int(0)
    print("was " + hex(a))
    cma.write_int(0, 0x12345678)
    a = cma.read_int(0)
    assert a == 0x12345678, "Memory isn't written or read correctly"
    print("now " + hex(a))
    del cma
    dg = DMAGPIO()
    dg.add_pulse(1 << pin, 4000)
    dg.add_delay(12000)
    dg.run(True)
    print("dmagpio is started")
    try:
        print("press enter to stop...")
        sys.stdin.readline()
    except KeyboardInterrupt:
        pass
    dg.stop()
    g.clear(pin)
    print("dma stopped")

if __name__ == "__main__":
    main()